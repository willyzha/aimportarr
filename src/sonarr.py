import logging
import httpx
from typing import List, Dict, Any, Optional
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
        self, folder: str, download_id: Optional[str] = None, series_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Query Sonarr for media files detected in the download directory."""
        params: Dict[str, Any] = {"folder": folder, "filterExistingFiles": False}
        if download_id:
            params["downloadId"] = download_id
        if series_id:
            params["seriesId"] = series_id

        try:
            return await self._get("manualimport", params=params)
        except Exception as e:
            logger.error(f"Failed to query /manualimport for folder '{folder}': {e}")
            return []

    async def trigger_manual_import(
        self,
        files_to_import: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
        category: ErrorCategory = ErrorCategory.OTHER
    ) -> bool:
        """Submit the resolved files to Sonarr for execution."""
        if not files_to_import:
            return False

        is_dry = self.categories.is_dry_run(category.value, self.dry_run) if dry_run is None else dry_run

        if is_dry:
            logger.info(f"[DRY RUN - Category: {category.value}] Would submit {len(files_to_import)} file(s) for ManualImport to Sonarr")
            for f in files_to_import:
                logger.info(f"[DRY RUN - Category: {category.value}]   -> File: {f.get('path')} | SeriesId: {f.get('seriesId')} | Episodes: {f.get('episodeIds')}")
            return True

        payload = {
            "name": "ManualImport",
            "files": files_to_import,
            "importMode": "auto"
        }
        try:
            res = await self._post("command", payload)
            logger.info(f"Triggered Sonarr ManualImport command successfully! Command ID: {res.get('id')}")
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
        status = item.get("trackedDownloadStatus", "")
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        series_info = item.get("series")

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        # Initial classification of the issue based on queue messages and series presence
        category = classify_issue(all_messages, has_media_info=bool(series_info))

        # If clean download with known series and no warnings/errors, skip
        if status not in ("warning", "error") and series_info and not all_messages and category == ErrorCategory.OTHER:
            return False

        # Verify whether this error category is enabled
        if not self.categories.is_enabled(category.value):
            logger.info(f"Category '{category.value}' is disabled in configuration. Skipping '{title}'.")
            return False

        is_cat_dry_run = self.categories.is_dry_run(category.value, self.dry_run)
        dry_run_tag = " [DRY RUN]" if is_cat_dry_run else ""
        logger.info(f"Found blocked/warning download in Sonarr queue: '{title}' (Category: {category.value}{dry_run_tag}, Status: {status})")
        logger.info(f"  Messages: {all_messages}")

        if not output_path:
            logger.warning(f"  Cannot process item '{title}': outputPath is empty.")
            return False

        folder_name = output_path.split("/")[-1]
        matched_from_library = False

        # If series is unknown, attempt to match release to Sonarr library using AI
        if not series_info:
            logger.info(f"  Item has no associated series (Unknown Media). Attempting to match against Sonarr library...")
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
            logger.info(f"  AI Library Match: matched_id={match_res.matched_id}, title='{match_res.matched_title}', conf={match_res.confidence:.2f}")
            logger.info(f"  Reasoning: {match_res.reasoning}")

            if not match_res.matched_id or match_res.confidence < self.min_confidence:
                logger.warning(f"  AI could not confidently match '{title}' to any series in your Sonarr library. Skipping.")
                return False

            series_info = next((s for s in all_series if s.get("id") == match_res.matched_id), None)
            if not series_info:
                logger.warning(f"  Series ID {match_res.matched_id} matched by AI not found in Sonarr library.")
                return False
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
            logger.info(f"  AI Series Verification: is_match={verify_res.is_match}, confidence={verify_res.confidence:.2f}")
            logger.info(f"  Reasoning: {verify_res.reasoning}")

            if not verify_res.is_match or verify_res.confidence < self.min_confidence:
                logger.warning(f"  AI rejected match or confidence too low ({verify_res.confidence:.2f} < {self.min_confidence}). Skipping.")
                return False

        # Query Sonarr manual import to inspect media files
        manual_items = await self.get_manual_import_items(
            folder=output_path, download_id=download_id, series_id=series_id
        )

        if not manual_items:
            logger.warning(f"  Sonarr /manualimport returned 0 media files for folder '{output_path}'.")
            return False

        # Refine issue category if manual import rejections offer more specific classification
        all_manual_rejections = [r.get("reason", "") for m in manual_items for r in m.get("rejections", [])]
        if all_manual_rejections:
            refined_cat = classify_issue(all_messages, has_media_info=bool(series_info), manual_rejections=all_manual_rejections)
            if refined_cat != category:
                logger.info(f"  Refined issue category from '{category.value}' to '{refined_cat.value}' based on /manualimport rejections.")
                category = refined_cat
                if not self.categories.is_enabled(category.value):
                    logger.info(f"Category '{category.value}' is disabled in configuration. Skipping.")
                    return False
                is_cat_dry_run = self.categories.is_dry_run(category.value, self.dry_run)

        files_to_import: List[Dict[str, Any]] = []
        all_episodes = await self.get_series_episodes(series_id) if series_id else []
        known_seasons = sorted(list(set(e.get("seasonNumber", 1) for e in all_episodes)))

        for m_file in manual_items:
            file_path = m_file.get("path", "")
            file_name = m_file.get("name", "")
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
                logger.info(f"  AI Sample Evaluation for '{file_name}': is_valid={sample_res.is_valid_media}, conf={sample_res.confidence:.2f}")
                logger.info(f"  Reasoning: {sample_res.reasoning}")
                if not sample_res.is_valid_media:
                    logger.info(f"  Skipping disposable sample/extra file: '{file_name}'")
                    continue

            # Check if Sonarr already identified episodes
            non_blocking_rejections = [
                r for r in rejections
                if "matched to series by id" not in r.lower()
                and not (category == ErrorCategory.QUALITY_OR_LANGUAGE and any(k in r.lower() for k in ["language not wanted", "quality not wanted", "custom format", "cutoff unmet", "higher quality"]))
            ]

            if curr_episodes and not non_blocking_rejections:
                ep_ids = [ep.get("id") for ep in curr_episodes]
                logger.info(f"  Pre-mapped file approved: '{file_name}' -> Episodes: {[ep.get('episodeNumber') for ep in curr_episodes]}")
                files_to_import.append({
                    "path": file_path,
                    "seriesId": target_series_id,
                    "episodeIds": ep_ids,
                    "quality": m_file.get("quality"),
                    "languages": m_file.get("languages", []),
                    "releaseGroup": m_file.get("releaseGroup"),
                })
            else:
                # Need AI to parse episode number
                logger.info(f"  Parsing unmapped file with AI: '{file_name}'")
                ep_match = await ai_provider.parse_episode(
                    file_name=file_name,
                    folder_name=file_folder,
                    series_title=candidate_title,
                    known_seasons=known_seasons,
                    known_episodes_in_season=all_episodes
                )
                logger.info(f"  AI Episode Result: Season {ep_match.season_number}, Episodes {ep_match.episode_numbers} (Conf: {ep_match.confidence:.2f})")
                logger.info(f"  Reasoning: {ep_match.reasoning}")

                if ep_match.confidence >= self.min_confidence and ep_match.episode_numbers:
                    matched_ep_ids = [
                        e.get("id") for e in all_episodes
                        if e.get("seasonNumber") == ep_match.season_number and e.get("episodeNumber") in ep_match.episode_numbers
                    ]
                    if matched_ep_ids:
                        files_to_import.append({
                            "path": file_path,
                            "seriesId": target_series_id,
                            "episodeIds": matched_ep_ids,
                            "quality": m_file.get("quality"),
                            "languages": m_file.get("languages", []),
                            "releaseGroup": m_file.get("releaseGroup"),
                        })
                    else:
                        logger.warning(f"  Episode numbers {ep_match.episode_numbers} for Season {ep_match.season_number} not found in Sonarr's database.")
                else:
                    logger.warning(f"  AI could not confidently identify episode for '{file_name}'.")

        if files_to_import:
            logger.info(f"Submitting {len(files_to_import)} file(s) for automated import to Sonarr (Category: {category.value})...")
            return await self.trigger_manual_import(files_to_import, dry_run=is_cat_dry_run, category=category)

        return False
