import logging
import httpx
from typing import List, Dict, Any, Optional
from .ai.base import BaseAIProvider

logger = logging.getLogger("aimportarr.sonarr")

class SonarrClient:
    def __init__(self, base_url: str, api_key: str, min_confidence: float = 0.85, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.dry_run = dry_run
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

    async def trigger_manual_import(self, files_to_import: List[Dict[str, Any]]) -> bool:
        """Submit the resolved files to Sonarr for execution."""
        if not files_to_import:
            return False

        if self.dry_run:
            logger.info(f"[DRY RUN] Would submit {len(files_to_import)} file(s) for ManualImport to Sonarr")
            for f in files_to_import:
                logger.info(f"[DRY RUN]   -> File: {f.get('path')} | SeriesId: {f.get('seriesId')} | Episodes: {f.get('episodeIds')}")
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
        Analyze a queued item. If blocked by ID match or unknown episode, use AI to resolve and import.
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

        # Check if this item is blocked by the specific Sonarr safety guardrail
        is_id_matched = any("release was matched to series by id" in msg.lower() for msg in all_messages)
        is_unknown_item = not series_info or any("unknown series" in msg.lower() or "unknown episode" in msg.lower() for msg in all_messages)

        if not (is_id_matched or is_unknown_item or status == "warning"):
            # Clean download or still in progress, skip
            return False

        logger.info(f"Found blocked/warning download in queue: '{title}' (Status: {status})")
        logger.info(f"  Messages: {all_messages}")

        if not output_path:
            logger.warning(f"  Cannot process item '{title}': outputPath is empty.")
            return False

        series_id = series_info.get("id") if series_info else None
        candidate_title = series_info.get("title") if series_info else ""
        year = series_info.get("year") if series_info else None
        alternate_titles = [a.get("title") for a in series_info.get("alternateTitles", [])] if series_info else []

        # If we have candidate series, verify with AI if release title matches candidate series
        if candidate_title:
            verify_res = await ai_provider.verify_series_match(
                release_title=title,
                folder_name=output_path.split("/")[-1],
                candidate_series_title=candidate_title,
                alternate_titles=alternate_titles,
                year=year
            )
            logger.info(f"  AI Series Verification: is_match={verify_res.is_match}, confidence={verify_res.confidence:.2f}")
            logger.info(f"  Reasoning: {verify_res.reasoning}")

            if not verify_res.is_match or verify_res.confidence < self.min_confidence:
                logger.warning(f"  AI rejected match or confidence too low ({verify_res.confidence:.2f} < {self.min_confidence}). Skipping.")
                return False

        # Query Sonarr manual import to see how Sonarr parsed the files inside
        manual_items = await self.get_manual_import_items(
            folder=output_path, download_id=download_id, series_id=series_id
        )

        if not manual_items:
            logger.warning(f"  Sonarr /manualimport returned 0 media files for folder '{output_path}'.")
            return False

        files_to_import: List[Dict[str, Any]] = []

        # Fetch episodes list for the series if needed
        all_episodes = await self.get_series_episodes(series_id) if series_id else []
        known_seasons = sorted(list(set(e.get("seasonNumber", 1) for e in all_episodes)))

        for m_file in manual_items:
            file_path = m_file.get("path", "")
            file_name = m_file.get("name", "")
            folder_name = m_file.get("folderName", "")
            rejections = [r.get("reason", "") for r in m_file.get("rejections", [])]
            curr_episodes = m_file.get("episodes", [])
            curr_series = m_file.get("series", {}) or series_info

            target_series_id = curr_series.get("id") if curr_series else series_id

            # Check if Sonarr already identified episodes, but only blocked on the ID rejection
            only_id_rejection = len(rejections) > 0 and all("release was matched to series by id" in r.lower() for r in rejections)

            if curr_episodes and (only_id_rejection or not rejections):
                # Episodes are already mapped accurately by Sonarr! Just need to clear the ID block.
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
                # Need AI to parse the episode number from the file name
                logger.info(f"  Parsing unmapped file with AI: '{file_name}'")
                ep_match = await ai_provider.parse_episode(
                    file_name=file_name,
                    folder_name=folder_name,
                    series_title=candidate_title,
                    known_seasons=known_seasons,
                    known_episodes_in_season=all_episodes
                )
                logger.info(f"  AI Episode Result: Season {ep_match.season_number}, Episodes {ep_match.episode_numbers} (Conf: {ep_match.confidence:.2f})")
                logger.info(f"  Reasoning: {ep_match.reasoning}")

                if ep_match.confidence >= self.min_confidence and ep_match.episode_numbers:
                    # Find matching episode IDs from all_episodes
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
            logger.info(f"Submitting {len(files_to_import)} file(s) for automated import to Sonarr...")
            return await self.trigger_manual_import(files_to_import)

        return False
