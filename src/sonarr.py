import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import httpx
from .ai.base import BaseAIProvider
from .classifier import classify_issue, ErrorCategory
from .config import CategoriesConfig

logger = logging.getLogger("aimportarr.sonarr")

class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        min_confidence: float = 0.85,
        dry_run: bool = False,
        categories: Optional[CategoriesConfig] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.dry_run = dry_run
        self.categories = categories or CategoriesConfig()
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

        is_dry = self.categories.is_dry_run(category.value, self.dry_run) if dry_run is None else dry_run

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

        # Never attempt to import an in-progress download
        if tracked_state in ("downloading", "importing", "imported"):
            return False

        # Output path is required for importing completed media files
        if not output_path or not output_path.strip() or output_path.strip() in (".", "/"):
            return False

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        category = classify_issue(all_messages, has_media_info=bool(series_info))

        if status not in ("warning", "error") and series_info and not all_messages and category == ErrorCategory.OTHER:
            return False

        if not self.categories.is_enabled(category.value):
            logger.debug(f"Skipping '{title}': category '{category.value}' is disabled in configuration.")
            return False

        is_cat_dry_run = self.categories.is_dry_run(category.value, self.dry_run)
        dry_run_tag = " [DRY RUN]" if is_cat_dry_run else ""
        logger.info(f"Processing blocked item: '{title}' [{category.value}{dry_run_tag}]")
        logger.debug(f"  Queue status: {status} (state: {tracked_state}) | Messages: {all_messages}")

        p = Path(output_path)
        folder_name = p.parent.name if p.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv") else (p.name or title)
        matched_from_library = False

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
                return False

            series_info = next((s for s in all_series if s.get("id") == match_res.matched_id), None)
            if not series_info:
                logger.warning(f"  Series ID {match_res.matched_id} matched by AI not found in Sonarr library.")
                return False

            logger.info(f"  AI matched release to library series: '{series_info.get('title')}' ({match_res.confidence:.0%})")
            logger.debug(f"  Library match reasoning: {match_res.reasoning}")
            matched_from_library = True

        series_id = series_info.get("id")
        candidate_title = series_info.get("title", "")
        year = series_info.get("year")
        alternate_titles = [a.get("title") for a in series_info.get("alternateTitles", [])]

        # Verify candidate series match with AI (unless already matched via library search)
        if not matched_from_library and candidate_title:
            verify_res = await ai_provider.verify_series_match(
                release_title=title,
                folder_name=folder_name,
                candidate_series_title=candidate_title,
                alternate_titles=alternate_titles,
                year=year
            )

            if not verify_res.is_match or verify_res.confidence < self.min_confidence:
                logger.warning(f"  AI rejected match for '{title}' against '{candidate_title}' ({verify_res.confidence:.0%}): {verify_res.reasoning}")
                return False

            logger.info(f"  AI verified series match: '{candidate_title}' ({verify_res.confidence:.0%})")
            logger.debug(f"  Series match reasoning: {verify_res.reasoning}")

        # Query Sonarr manual import to inspect media files
        manual_items = await self.get_manual_import_items(
            folder=output_path, download_id=download_id
        )

        if not manual_items:
            logger.warning(f"  Sonarr returned 0 importable files for '{title}'.")
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
                is_cat_dry_run = self.categories.is_dry_run(category.value, self.dry_run)

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
                if not sample_res.is_valid_media:
                    logger.info(f"  Skipping disposable sample/extra file: '{file_name}'")
                    logger.debug(f"  Sample evaluation reasoning: {sample_res.reasoning}")
                    continue
                else:
                    logger.debug(f"  Valid media confirmed for '{file_name}': {sample_res.reasoning}")

            # Check if Sonarr already identified episodes
            non_blocking_rejections = [
                r for r in rejections
                if "matched to series by id" not in r.lower()
                and not (category == ErrorCategory.QUALITY_OR_LANGUAGE and any(k in r.lower() for k in ["language not wanted", "quality not wanted", "custom format", "cutoff unmet", "higher quality"]))
            ]

            if curr_episodes and not non_blocking_rejections:
                ep_ids = [ep.get("id") for ep in curr_episodes]
                ep_nums = [ep.get("episodeNumber") for ep in curr_episodes]
                logger.info(f"  Approved file: '{file_name}' -> Episode(s) {ep_nums}")
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
                        logger.info(f"  AI mapped '{file_name}' -> Season {ep_match.season_number}, Episode(s) {ep_match.episode_numbers} ({ep_match.confidence:.0%})")
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
                    logger.warning(f"  AI could not identify episode for '{file_name}' ({ep_match.confidence:.0%}): {ep_match.reasoning}")

        if files_to_import:
            return await self.trigger_manual_import(files_to_import, dry_run=is_cat_dry_run, category=category)

        return False
