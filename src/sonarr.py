import asyncio
import logging
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
import httpx
from .ai.base import BaseAIProvider, MediaMatchResult, LibraryMatchResult, MalwareEvaluationResult
from .classifier import classify_issue, ErrorCategory
from .config import CategoriesConfig, ExtractionConfig
from .extractor import find_archive_file, inspect_archive, extract_archive, resolve_local_path
from .metrics import metrics

logger = logging.getLogger("aimportarr.sonarr")

class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        min_confidence: float = 0.85,
        dry_run: Optional[bool] = None,
        categories: Optional[CategoriesConfig] = None,
        extraction_config: Optional[ExtractionConfig] = None,
        path_mappings: Optional[List[Any]] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.dry_run = dry_run
        self.categories = categories or CategoriesConfig()
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
        json: Optional[Dict[str, Any]] = None
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
        data = await self._get("queue", params={
            "pageSize": 100,
            "includeUnknownSeriesItems": True,
            "includeSeries": True,
            "includeEpisode": True,
        })
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
        return await self._get("episode", params={"seriesId": series_id})

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
        folder_dir = str(p.parent) if p.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv") else str(p)
        params: Dict[str, Any] = {"folder": folder_dir, "filterExistingFiles": True}
        if download_id:
            params["downloadId"] = download_id

        try:
            return await self._get("manualimport", params=params)
        except Exception as e:
            logger.error(f"Failed to query /manualimport for folder '{folder_dir}': {e}")
            return []

    async def trigger_manual_import(
        self,
        files_to_import: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
        category: ErrorCategory = ErrorCategory.OTHER
    ) -> bool:
        """Submit the resolved files to Sonarr for execution and track command status."""
        if not files_to_import:
            return False

        is_dry = self.categories.is_dry_run(category.value) if dry_run is None else dry_run

        if is_dry:
            logger.info(f"[DRY RUN] Would import {len(files_to_import)} file(s) to Sonarr [{category.value}]")
            for f in files_to_import:
                logger.debug(f"  [DRY RUN] File: {f.get('path')} | SeriesId: {f.get('seriesId')} | Episodes: {f.get('episodeIds')}")
            return True

        payload = {
            "name": "ManualImport",
            "files": files_to_import,
            "importMode": "auto"
        }
        try:
            logger.info(f"Submitting {len(files_to_import)} file(s) to Sonarr for import [{category.value}]...")
            res = await self._post("command", payload)
            cmd_id = res.get("id")
            logger.debug(f"Triggered Sonarr ManualImport command ID: {cmd_id}")

            if cmd_id:
                # Poll command status up to 10 seconds to confirm execution
                for _ in range(5):
                    await asyncio.sleep(2)
                    try:
                        cmd_status = await self._get(f"command/{cmd_id}")
                        state = cmd_status.get("status") or cmd_status.get("state")
                        if state == "completed":
                            logger.info(f"Sonarr successfully imported {len(files_to_import)} file(s) (Command ID: {cmd_id})")
                            return True
                        elif state == "failed":
                            logger.error(f"Sonarr ManualImport command {cmd_id} failed: {cmd_status.get('message', 'Unknown failure')}")
                            return False
                    except Exception as pe:
                        logger.debug(f"Failed to poll command {cmd_id} status: {pe}")
                        break

            return True
        except Exception as e:
            logger.error(f"Failed to trigger Sonarr ManualImport command: {e}")
            return False

    async def dismiss_queue_warning(
        self,
        queue_id: int,
        download_id: Optional[str] = None,
        dry_run: bool = False,
        title: str = ""
    ) -> bool:
        """
        Dismiss a stuck queue warning from Sonarr (e.g. missing episodes from a season pack).
        Removes the queue record(s) without removing the torrent from the download client.
        """
        ids_to_remove = [queue_id]
        if download_id:
            try:
                current_queue = await self.get_queue()
                matching_ids = [
                    q.get("id") for q in current_queue
                    if q.get("downloadId") == download_id and q.get("id")
                ]
                if matching_ids:
                    ids_to_remove = list(set(ids_to_remove + matching_ids))
            except Exception as e:
                logger.debug(f"Failed to lookup all queue items for downloadId '{download_id}': {e}")

        if dry_run:
            logger.info(f"[DRY RUN] Would dismiss warning from Sonarr queue for '{title}' ({len(ids_to_remove)} queue item(s)) [missing_episodes]")
            if download_id:
                self._dismissed_downloads.add(download_id)
            return True

        logger.info(f"Dismissing warning from Sonarr queue for '{title}' ({len(ids_to_remove)} queue item(s)) [missing_episodes]...")
        try:
            if len(ids_to_remove) > 1:
                try:
                    await self._delete(
                        "queue/bulk",
                        params={"removeFromClient": "false", "blocklist": "false"},
                        json={"ids": ids_to_remove}
                    )
                except Exception as be:
                    logger.debug(f"Bulk queue delete failed, falling back to individual deletes: {be}")
                    for qid in ids_to_remove:
                        try:
                            await self._delete(
                                f"queue/{qid}",
                                params={"removeFromClient": "false", "blocklist": "false"}
                            )
                        except Exception:
                            pass
            else:
                await self._delete(
                    f"queue/{queue_id}",
                    params={"removeFromClient": "false", "blocklist": "false"}
                )

            logger.info(f"Successfully dismissed queue warning for '{title}' (Queue IDs: {ids_to_remove})")
            if download_id:
                self._dismissed_downloads.add(download_id)
            return True
        except Exception as e:
            logger.error(f"Failed to dismiss queue warning for '{title}': {e}")
            return False

    async def delete_and_blocklist(
        self,
        queue_id: int,
        download_id: Optional[str] = None,
        dry_run: bool = False,
        title: str = ""
    ) -> bool:
        """
        Delete a fake/malware release from Sonarr and the download client, and add it to the blocklist.
        """
        ids_to_remove = [queue_id]
        if download_id:
            try:
                current_queue = await self.get_queue()
                matching_ids = [
                    q.get("id") for q in current_queue
                    if q.get("downloadId") == download_id and q.get("id")
                ]
                if matching_ids:
                    ids_to_remove = list(set(ids_to_remove + matching_ids))
            except Exception as e:
                logger.debug(f"Failed to lookup all queue items for downloadId '{download_id}': {e}")

        if dry_run:
            logger.info(f"  [DRY RUN] Would delete and blocklist fake/malware release '{title}' (Queue IDs: {ids_to_remove}) from download client and queue.")
            if download_id:
                self._dismissed_downloads.add(download_id)
            return True

        logger.info(f"  🗑 Deleting and blocklisting fake/malware release '{title}' (Queue IDs: {ids_to_remove}) from download client and queue...")
        try:
            if len(ids_to_remove) > 1:
                try:
                    await self._delete(
                        "queue/bulk",
                        params={"removeFromClient": "true", "blocklist": "true"},
                        json={"ids": ids_to_remove}
                    )
                except Exception as be:
                    logger.debug(f"Bulk delete failed, falling back to individual deletes: {be}")
                    for qid in ids_to_remove:
                        try:
                            await self._delete(
                                f"queue/{qid}",
                                params={"removeFromClient": "true", "blocklist": "true"}
                            )
                        except Exception:
                            pass
            else:
                await self._delete(
                    f"queue/{queue_id}",
                    params={"removeFromClient": "true", "blocklist": "true"}
                )

            logger.info(f"  ✔ Successfully deleted and blocklisted fake release: '{title}'")
            if download_id:
                self._dismissed_downloads.add(download_id)
            return True
        except Exception as e:
            logger.error(f"  Failed to delete and blocklist '{title}': {e}")
            return False

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

    async def process_queue_item(self, item: Dict[str, Any], ai_provider: BaseAIProvider) -> bool:
        """
        Analyze a queued item. Classify the issue, check category dry_run/enabled settings,
        and use AI to resolve and import.
        """
        title = item.get("title", "")
        status = (item.get("trackedDownloadStatus") or "").lower()
        tracked_state = (item.get("trackedDownloadState") or "").lower()
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        series_info = item.get("series")

        # Skip if this download was already dismissed in the current session
        if download_id and download_id in self._dismissed_downloads:
            logger.debug(f"Skipping '{title}': downloadId '{download_id}' was already dismissed.")
            return True

        # Skip if already attempted (persisted in SQLite to prevent retries, resets on UI Retry click)
        item_keys = self._get_item_keys(item)
        if metrics.is_item_attempted(item_keys):
            logger.debug(f"Skipping '{title}': previously attempted (will not retry until Retry is clicked in UI or retention expires).")
            return False

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        # Never touch downloads in normal non-blocked states:
        # downloading: active in torrent client
        # importing / imported: Sonarr is actively processing the import
        # queued, delay, paused: waiting in line
        if tracked_state in ("downloading", "importing", "imported", "queued", "delay", "paused"):
            return False

        # If importpending without any warning/error or status messages, let Sonarr import naturally
        if tracked_state == "importpending" and status not in ("warning", "error") and not all_messages:
            return False

        # Output path is required for importing completed media files
        if not output_path or not output_path.strip() or output_path.strip() in (".", "/"):
            return False

        # Ignore stalled downloads with 0 connections (handled by torrent client daemon, not an import failure)
        error_msg = (item.get("errorMessage") or "").lower()
        if "stalled" in error_msg and ("no connections" in error_msg or not all_messages):
            return False
        if any("stalled with no connections" in m.lower() for m in all_messages):
            return False

        category = classify_issue(all_messages, has_media_info=bool(series_info), title=title)

        # If Sonarr has not flagged an issue and item is not importBlocked, let Sonarr complete normally
        if status not in ("warning", "error") and tracked_state != "importblocked" and not all_messages and series_info:
            return False

        if not self.categories.is_enabled(category.value):
            logger.debug(f"Skipping '{title}': category '{category.value}' is disabled in configuration.")
            return False

        is_cat_dry_run = self.categories.is_dry_run(category.value)
        mode_tag = "[DRY RUN]" if is_cat_dry_run else "[LIVE]"
        logger.info(f"Processing blocked item: '{title}' [{category.value}] {mode_tag}")
        logger.debug(f"  Queue status: {status} (state: {tracked_state}) | Messages: {all_messages}")

        # Check for archive files on disk if extraction is enabled
        staging_dir_to_clean: Optional[Path] = None
        if self.extraction_config.enabled and output_path:
            p_local = resolve_local_path(output_path, self.path_mappings)
            archive_file = find_archive_file(p_local)
            if archive_file:
                logger.info(f"  🔍 Archive detected: '{archive_file.name}'. Inspecting contents in memory...")
                inspection = await inspect_archive(archive_file)
                if inspection:
                    if inspection.has_suspicious_files or not inspection.has_video_files:
                        suspicious_names = [f.path for f in inspection.suspicious_files]
                        logger.warning(
                            f"  Archive '{archive_file.name}' contains suspicious files or 0 video files ({suspicious_names}). Evaluating with AI..."
                        )
                        fake_res = await ai_provider.evaluate_fake_or_malware(
                            release_title=title,
                            file_name=archive_file.name,
                            error_messages=all_messages,
                            archive_files=[e.path for e in inspection.files]
                        )
                        model_label = fake_res.model_name or ai_provider.name
                        if fake_res.is_fake_or_malware and fake_res.confidence >= self.min_confidence:
                            logger.warning(
                                f"  🚨 AI confirmed fake/malware archive: '{title}' | Model: {model_label} | Confidence: {fake_res.confidence:.0%}"
                            )
                            logger.warning(f"  Reasoning: {fake_res.reasoning}")
                            metrics.record_processed_item(
                                app="Sonarr",
                                title=title,
                                category="fake_or_malware",
                                status="dry_run" if is_cat_dry_run else "blocked",
                                confidence=fake_res.confidence,
                                model=model_label,
                                reasoning=fake_res.reasoning,
                                details=f"Blocklisted scam archive ({archive_file.name})",
                                item_keys=item_keys,
                            )
                            queue_id = item.get("id")
                            if queue_id:
                                return await self.delete_and_blocklist(
                                    queue_id=queue_id,
                                    download_id=download_id,
                                    dry_run=is_cat_dry_run,
                                    title=title
                                )
                            return False
                        else:
                            logger.info(f"  AI did not confirm '{title}' as fake/malware: {fake_res.reasoning}")
                    elif inspection.has_video_files and not inspection.has_suspicious_files:
                        logger.info(f"  📦 Valid media archive detected ({len(inspection.video_files)} video file(s)). Extracting in sandbox...")
                        staging_dir = archive_file.parent / f"{archive_file.stem}{self.extraction_config.staging_suffix}"
                        if is_cat_dry_run:
                            logger.info(f"  [DRY RUN] Would extract '{archive_file.name}' to sandbox '{staging_dir}'")
                        else:
                            extract_ok = await extract_archive(archive_file, staging_dir)
                            if not extract_ok:
                                logger.error(f"  Failed to extract archive '{archive_file.name}'")
                                return False
                            metrics.record_archive_extracted()
                            logger.info(f"  ✔ Extraction complete in sandbox: '{staging_dir}'. Scanning for import...")
                            output_path = str(staging_dir)
                            if self.extraction_config.delete_after_import:
                                staging_dir_to_clean = staging_dir

        # Handle fake or malware releases (from queue status messages)
        if category == ErrorCategory.FAKE_OR_MALWARE:
            file_name = Path(output_path).name if output_path else title
            logger.info(f"  Evaluating potential fake/malware release with AI: '{title}'...")
            fake_res = await ai_provider.evaluate_fake_or_malware(
                release_title=title,
                file_name=file_name,
                error_messages=all_messages
            )
            model_label = fake_res.model_name or ai_provider.name
            if fake_res.is_fake_or_malware and fake_res.confidence >= self.min_confidence:
                logger.warning(
                    f"  🚨 AI confirmed fake/malware release: '{title}' | Model: {model_label} | Confidence: {fake_res.confidence:.0%}"
                )
                logger.warning(f"  Reasoning: {fake_res.reasoning}")
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="fake_or_malware",
                    status="dry_run" if is_cat_dry_run else "blocked",
                    confidence=fake_res.confidence,
                    model=model_label,
                    reasoning=fake_res.reasoning,
                    details="Blocklisted scam/malware release from queue status",
                    item_keys=item_keys,
                )
                queue_id = item.get("id")
                if queue_id:
                    return await self.delete_and_blocklist(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_cat_dry_run,
                        title=title
                    )
                return False
            else:
                logger.info(
                    f"  AI did not confirm '{title}' as fake/malware (Model: {model_label}, Confidence: {fake_res.confidence:.0%}): {fake_res.reasoning}. Skipping."
                )
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="fake_or_malware",
                    status="skipped",
                    confidence=fake_res.confidence,
                    model=model_label,
                    reasoning=fake_res.reasoning,
                    details="AI determined release is not malware",
                    item_keys=item_keys,
                )
                return False

        p = Path(output_path)
        folder_name = p.parent.name if p.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv") else (p.name or title)
        matched_from_library = False
        match_res: Optional[LibraryMatchResult] = None

        # If series is unknown, attempt to match release to Sonarr library using AI
        if not series_info:
            logger.info(f"  Searching Sonarr library to identify unknown release '{title}'...")
            all_series = await self.get_all_series()
            if not all_series:
                logger.warning("  Could not retrieve library series from Sonarr.")
                return False

            candidates = [
                {
                    "id": s.get("id"),
                    "title": s.get("title"),
                    "year": s.get("year"),
                    "aliases": [a.get("title") for a in s.get("alternateTitles", [])]
                }
                for s in all_series
            ]
            match_res = await ai_provider.match_unknown_media(
                release_title=title,
                folder_name=folder_name,
                media_type="series",
                candidates=candidates
            )

            if not match_res.matched_id or match_res.confidence < self.min_confidence:
                logger.warning(f"  Could not match '{title}' to any series in Sonarr library ({match_res.confidence:.0%}): {match_res.reasoning}")
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="unknown_media",
                    status="rejected",
                    confidence=match_res.confidence,
                    model=match_res.model_name or ai_provider.name,
                    reasoning=match_res.reasoning,
                    details="Could not match release to Sonarr library",
                    item_keys=item_keys,
                )
                return False

            series_info = next((s for s in all_series if s.get("id") == match_res.matched_id), None)
            if not series_info:
                logger.warning(f"  Series ID {match_res.matched_id} matched by AI not found in Sonarr library.")
                return False

            model_label = match_res.model_name or ai_provider.name
            logger.info(f"  ✔ AI matched library series: '{series_info.get('title')}' | Model: {model_label} | Confidence: {match_res.confidence:.0%}")
            logger.debug(f"  Library match reasoning: {match_res.reasoning}")
            matched_from_library = True

        series_id = series_info.get("id")
        candidate_title = series_info.get("title", "")
        year = series_info.get("year")
        alternate_titles = [a.get("title") for a in series_info.get("alternateTitles", [])]

        # If the series was already confirmed by Sonarr's title parser or AI library match,
        # we don't re-verify it. However, if Sonarr blocked the item because it was matched by ID
        # (ID_MATCH), we will use AI below to verify that the release actually belongs to this series.

        # Query Sonarr manual import to inspect media files
        manual_items = await self.get_manual_import_items(
            folder=output_path, download_id=download_id
        )

        if not manual_items:
            if category == ErrorCategory.MISSING_EPISODES:
                queue_id = item.get("id")
                if queue_id:
                    logger.info(f"  Sonarr returned 0 remaining importable files for '{title}'. Dismissing missing episodes warning from queue...")
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category="missing_episodes",
                        status="dry_run" if is_cat_dry_run else "dismissed",
                        confidence=1.0,
                        model=None,
                        reasoning="All available files accounted for; queue warning dismissed",
                        details="Dismissed missing episodes queue warning",
                        item_keys=item_keys,
                    )
                    return await self.dismiss_queue_warning(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_cat_dry_run,
                        title=title
                    )
            logger.warning(f"  Sonarr returned 0 importable files for '{title}'.")
            metrics.record_processed_item(
                app="Sonarr",
                title=title,
                category=category.value,
                status="rejected",
                confidence=0.0,
                model=ai_provider.name,
                reasoning="Sonarr manual import returned 0 importable files",
                details="0 files found",
                item_keys=item_keys,
            )
            return False

        # Refine issue category if manual import rejections offer more specific classification
        all_manual_rejections = [r.get("reason", "") for m in manual_items for r in m.get("rejections", [])]
        if all_manual_rejections:
            refined_cat = classify_issue(all_messages, has_media_info=bool(series_info), manual_rejections=all_manual_rejections)
            if refined_cat != category:
                logger.debug(f"  Refined issue category from '{category.value}' to '{refined_cat.value}' based on manual import rejections.")
                category = refined_cat
                if not self.categories.is_enabled(category.value):
                    logger.debug(f"Skipping '{title}': refined category '{category.value}' is disabled.")
                    return False
                is_cat_dry_run = self.categories.is_dry_run(category.value)

        # If matched by ID, verify with AI that the release actually belongs to the series
        series_verify_res: Optional[MediaMatchResult] = None
        if category == ErrorCategory.ID_MATCH:
            logger.info(f"  Verifying release identity with AI for '{title}' against '{candidate_title}' ({year or 'Unknown Year'})...")
            series_verify_res = await ai_provider.verify_series_match(
                release_title=title,
                folder_name=folder_name,
                candidate_series_title=candidate_title,
                alternate_titles=alternate_titles,
                year=year
            )
            model_label = series_verify_res.model_name or ai_provider.name
            if not series_verify_res.is_match or series_verify_res.confidence < self.min_confidence:
                logger.warning(
                    f"  ✖ AI rejected series match for '{title}' against '{candidate_title}' "
                    f"| Model: {model_label} | Confidence: {series_verify_res.confidence:.0%} | Reasoning: {series_verify_res.reasoning}"
                )
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="id_match",
                    status="rejected",
                    confidence=series_verify_res.confidence,
                    model=model_label,
                    reasoning=series_verify_res.reasoning,
                    details=f"AI rejected series match against '{candidate_title}'",
                    item_keys=item_keys,
                )
                return False

            logger.info(
                f"  ✔ AI confirmed series match: '{candidate_title}' | Model: {model_label} | Confidence: {series_verify_res.confidence:.0%}"
            )
            logger.debug(f"  Series match reasoning: {series_verify_res.reasoning}")

        files_to_import: List[Dict[str, Any]] = []
        all_episodes = await self.get_series_episodes(series_id) if series_id else []
        known_seasons = sorted(list(set(e.get("seasonNumber", 1) for e in all_episodes)))
        series_path = (series_info.get("path") or "").rstrip("/") if series_info else ""

        for m_file in manual_items:
            file_path = m_file.get("path", "")

            # Safeguard: never import files that are already inside the destination library folder
            if series_path and file_path.startswith(series_path):
                logger.debug(f"Ignoring file '{file_path}' because it is inside destination library directory '{series_path}'")
                continue

            file_name = m_file.get("name") or m_file.get("relativePath") or Path(file_path).name
            file_folder = m_file.get("folderName", "") or folder_name
            rejections = [r.get("reason", "") for r in m_file.get("rejections", [])]
            curr_episodes = m_file.get("episodes", [])
            curr_series = m_file.get("series", {}) or series_info
            target_series_id = curr_series.get("id") if curr_series else series_id

            # Sample or extra evaluation
            is_sample_rej = any("sample" in r.lower() or "short video" in r.lower() for r in rejections)
            if is_sample_rej or category == ErrorCategory.SAMPLE_OR_EXTRA:
                sample_res = await ai_provider.evaluate_sample_or_extra(
                    file_name=file_name,
                    folder_name=file_folder,
                    media_title=candidate_title
                )
                sample_decider = sample_res.model_name or ai_provider.name
                if not sample_res.is_valid_media:
                    logger.info(f"  ✖ Skipping sample/extra: '{file_name}' | Model: {sample_decider} | Confidence: {sample_res.confidence:.0%}")
                    logger.debug(f"  Sample evaluation reasoning: {sample_res.reasoning}")
                    continue
                else:
                    logger.debug(f"  ✔ Valid media confirmed: '{file_name}' | Model: {sample_decider} | Confidence: {sample_res.confidence:.0%}")

            # Check if Sonarr already identified episodes
            non_blocking_rejections = [
                r for r in rejections
                if "matched to series by id" not in r.lower()
                and not (category == ErrorCategory.QUALITY_OR_LANGUAGE and any(k in r.lower() for k in ["language not wanted", "quality not wanted", "custom format", "cutoff unmet", "higher quality"]))
            ]

            if curr_episodes and not non_blocking_rejections:
                ep_ids = [ep.get("id") for ep in curr_episodes]
                ep_nums = [ep.get("episodeNumber") for ep in curr_episodes]
                if matched_from_library and match_res:
                    decider = match_res.model_name or ai_provider.name
                    conf_str = f"{match_res.confidence:.0%}"
                elif series_verify_res:
                    decider = series_verify_res.model_name or ai_provider.name
                    conf_str = f"{series_verify_res.confidence:.0%}"
                elif category == ErrorCategory.MISSING_EPISODES:
                    decider = "Sonarr (Season Pack Match)"
                    conf_str = "100%"
                else:
                    decider = "Sonarr (Direct Match)"
                    conf_str = "100%"

                logger.info(f"  ✔ Approved: '{file_name}' -> Episode(s) {ep_nums} | Model: {decider} | Confidence: {conf_str}")
                file_payload = {
                    "path": file_path,
                    "seriesId": target_series_id,
                    "episodeIds": ep_ids,
                    "quality": m_file.get("quality"),
                    "languages": m_file.get("languages", []),
                    "releaseGroup": m_file.get("releaseGroup"),
                }
                if download_id:
                    file_payload["downloadId"] = download_id
                files_to_import.append(file_payload)
            else:
                # Need AI to parse episode number
                logger.info(f"  Parsing unmapped file with AI: '{file_name}'...")
                ep_match = await ai_provider.parse_episode(
                    file_name=file_name,
                    folder_name=file_folder,
                    series_title=candidate_title,
                    known_seasons=known_seasons,
                    known_episodes_in_season=all_episodes
                )

                if ep_match.confidence >= self.min_confidence and ep_match.episode_numbers:
                    matched_ep_ids = [
                        e.get("id") for e in all_episodes
                        if e.get("seasonNumber") == ep_match.season_number and e.get("episodeNumber") in ep_match.episode_numbers
                    ]
                    if matched_ep_ids:
                        decider = ep_match.model_name or ai_provider.name
                        conf_str = f"{ep_match.confidence:.0%}"
                        logger.info(f"  ✔ Approved: '{file_name}' -> Season {ep_match.season_number}, Episode(s) {ep_match.episode_numbers} | Model: {decider} | Confidence: {conf_str}")
                        logger.debug(f"  Episode parsing reasoning: {ep_match.reasoning}")
                        file_payload = {
                            "path": file_path,
                            "seriesId": target_series_id,
                            "episodeIds": matched_ep_ids,
                            "quality": m_file.get("quality"),
                            "languages": m_file.get("languages", []),
                            "releaseGroup": m_file.get("releaseGroup"),
                        }
                        if download_id:
                            file_payload["downloadId"] = download_id
                        files_to_import.append(file_payload)
                    else:
                        logger.warning(f"  Episodes {ep_match.episode_numbers} for Season {ep_match.season_number} not found in Sonarr's database.")
                else:
                    decider = ep_match.model_name or ai_provider.name
                    conf_str = f"{ep_match.confidence:.0%}"
                    logger.warning(f"  ✖ Rejected file: '{file_name}' | Model: {decider} | Confidence: {conf_str} | Reasoning: {ep_match.reasoning}")

        if files_to_import:
            import_ok = await self.trigger_manual_import(files_to_import, dry_run=is_cat_dry_run, category=category)
            if import_ok and staging_dir_to_clean and not is_cat_dry_run:
                try:
                    shutil.rmtree(staging_dir_to_clean, ignore_errors=True)
                    logger.info(f"  🧹 Cleaned up extracted staging folder '{staging_dir_to_clean}'. Seeding archive preserved.")
                except Exception as ce:
                    logger.debug(f"Failed to clean up staging folder '{staging_dir_to_clean}': {ce}")

            # Record metrics
            decider_model = None
            decider_conf = 1.0
            decider_reasoning = f"Imported {len(files_to_import)} file(s)"
            if match_res:
                decider_model = match_res.model_name or ai_provider.name
                decider_conf = match_res.confidence
                decider_reasoning = match_res.reasoning
            elif series_verify_res:
                decider_model = series_verify_res.model_name or ai_provider.name
                decider_conf = series_verify_res.confidence
                decider_reasoning = series_verify_res.reasoning

            status_label = "dry_run" if is_cat_dry_run else ("imported" if import_ok else "failed")
            metrics.record_processed_item(
                app="Sonarr",
                title=title,
                category=category.value,
                status=status_label,
                confidence=decider_conf,
                model=decider_model,
                reasoning=decider_reasoning,
                details=f"Imported {len(files_to_import)} episode file(s) into '{candidate_title}'",
                item_keys=item_keys,
            )

            if import_ok and category == ErrorCategory.MISSING_EPISODES:
                queue_id = item.get("id")
                if queue_id:
                    await self.dismiss_queue_warning(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_cat_dry_run,
                        title=title
                    )
            return import_ok

        # If no files left to import, but this was flagged as missing_episodes:
        if category == ErrorCategory.MISSING_EPISODES:
            queue_id = item.get("id")
            if queue_id:
                logger.info(f"  All available files for '{title}' are accounted for. Dismissing missing episodes warning from Sonarr queue...")
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="missing_episodes",
                    status="dry_run" if is_cat_dry_run else "dismissed",
                    confidence=1.0,
                    model=None,
                    reasoning="All available files accounted for; queue warning dismissed",
                    details="Dismissed missing episodes queue warning",
                    item_keys=item_keys,
                )
                return await self.dismiss_queue_warning(
                    queue_id=queue_id,
                    download_id=download_id,
                    dry_run=is_cat_dry_run,
                    title=title
                )

        metrics.record_processed_item(
            app="Sonarr",
            title=title,
            category=category.value,
            status="rejected",
            confidence=0.0,
            model=ai_provider.name,
            reasoning="No files available or approved for import",
            details="0 files imported",
            item_keys=item_keys,
        )
        return False
