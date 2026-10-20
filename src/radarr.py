import logging
import httpx
from typing import List, Dict, Any, Optional
from .ai.base import BaseAIProvider

logger = logging.getLogger("aimportarr.radarr")

class RadarrClient:
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
        """Fetch all items from the Radarr download queue."""
        data = await self._get("queue", params={
            "pageSize": 100,
            "includeUnknownMovieItems": True,
            "includeMovie": True,
        })
        return data.get("records", [])

    async def get_movie(self, movie_id: int) -> Dict[str, Any]:
        """Fetch detailed movie metadata."""
        return await self._get(f"movie/{movie_id}")

    async def get_manual_import_items(
        self, folder: str, download_id: Optional[str] = None, movie_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Query Radarr for media files detected in the download directory."""
        params: Dict[str, Any] = {"folder": folder, "filterExistingFiles": False}
        if download_id:
            params["downloadId"] = download_id
        if movie_id:
            params["movieId"] = movie_id

        try:
            return await self._get("manualimport", params=params)
        except Exception as e:
            logger.error(f"Failed to query Radarr /manualimport for folder '{folder}': {e}")
            return []

    async def trigger_manual_import(self, files_to_import: List[Dict[str, Any]]) -> bool:
        """Submit resolved movie files to Radarr for execution."""
        if not files_to_import:
            return False

        if self.dry_run:
            logger.info(f"[DRY RUN] Would submit {len(files_to_import)} movie file(s) for ManualImport to Radarr")
            for f in files_to_import:
                logger.info(f"[DRY RUN]   -> File: {f.get('path')} | MovieId: {f.get('movieId')}")
            return True

        payload = {
            "name": "ManualImport",
            "files": files_to_import,
            "importMode": "auto"
        }
        try:
            res = await self._post("command", payload)
            logger.info(f"Triggered Radarr ManualImport command successfully! Command ID: {res.get('id')}")
            return True
        except Exception as e:
            logger.error(f"Failed to trigger Radarr ManualImport command: {e}")
            return False

    async def process_queue_item(self, item: Dict[str, Any], ai_provider: BaseAIProvider) -> bool:
        """
        Analyze a queued movie item. If blocked by ID match or unknown movie, use AI to verify and import.
        """
        title = item.get("title", "")
        status = item.get("trackedDownloadStatus", "")
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        movie_info = item.get("movie")

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        # Check if this item is blocked by Radarr's safety guardrail or title mismatch
        is_id_matched = any("release was matched to movie by id" in msg.lower() for msg in all_messages)
        is_unknown_item = not movie_info or any("unknown movie" in msg.lower() or "no movie was found" in msg.lower() for msg in all_messages)

        if not (is_id_matched or is_unknown_item or status == "warning"):
            return False

        logger.info(f"Found blocked/warning movie in Radarr queue: '{title}' (Status: {status})")
        logger.info(f"  Messages: {all_messages}")

        if not output_path:
            logger.warning(f"  Cannot process movie '{title}': outputPath is empty.")
            return False

        movie_id = movie_info.get("id") if movie_info else None
        candidate_title = movie_info.get("title") if movie_info else ""
        year = movie_info.get("year") if movie_info else None
        alternate_titles = [a.get("title") for a in movie_info.get("alternateTitles", [])] if movie_info else []

        if not candidate_title:
            logger.warning(f"  No candidate movie associated with release '{title}'. Skipping.")
            return False

        # Verify with AI if release title matches candidate movie
        verify_res = await ai_provider.verify_movie_match(
            release_title=title,
            folder_name=output_path.split("/")[-1],
            candidate_movie_title=candidate_title,
            alternate_titles=alternate_titles,
            year=year
        )
        logger.info(f"  AI Movie Verification: is_match={verify_res.is_match}, confidence={verify_res.confidence:.2f}")
        logger.info(f"  Reasoning: {verify_res.reasoning}")

        if not verify_res.is_match or verify_res.confidence < self.min_confidence:
            logger.warning(f"  AI rejected movie match or confidence too low ({verify_res.confidence:.2f} < {self.min_confidence}). Skipping.")
            return False

        # Query Radarr manual import to get media items
        manual_items = await self.get_manual_import_items(
            folder=output_path, download_id=download_id, movie_id=movie_id
        )

        if not manual_items:
            logger.warning(f"  Radarr /manualimport returned 0 media files for folder '{output_path}'.")
            return False

        files_to_import: List[Dict[str, Any]] = []
        for m_file in manual_items:
            file_path = m_file.get("path", "")
            curr_movie = m_file.get("movie") or movie_info
            target_movie_id = curr_movie.get("id") if curr_movie else movie_id

            files_to_import.append({
                "path": file_path,
                "movieId": target_movie_id,
                "quality": m_file.get("quality"),
                "languages": m_file.get("languages", []),
                "releaseGroup": m_file.get("releaseGroup"),
            })

        if files_to_import:
            logger.info(f"Submitting {len(files_to_import)} movie file(s) for automated import to Radarr...")
            return await self.trigger_manual_import(files_to_import)

        return False
