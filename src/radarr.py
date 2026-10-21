import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import httpx
from .ai.base import BaseAIProvider
from .classifier import classify_issue, ErrorCategory
from .config import CategoriesConfig

logger = logging.getLogger("aimportarr.radarr")

class RadarrClient:
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
        folder_dir = str(p.parent) if p.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv") else str(p)
        params: Dict[str, Any] = {"folder": folder_dir, "filterExistingFiles": True}
        if download_id:
            params["downloadId"] = download_id

        try:
            return await self._get("manualimport", params=params)
        except Exception as e:
            logger.error(f"Failed to query Radarr /manualimport for folder '{folder_dir}': {e}")
            return []

    async def trigger_manual_import(
        self,
        files_to_import: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
        category: ErrorCategory = ErrorCategory.OTHER
    ) -> bool:
        """Submit resolved movie files to Radarr for execution and track command status."""
        if not files_to_import:
            return False

        is_dry = self.categories.is_dry_run(category.value, self.dry_run) if dry_run is None else dry_run

        if is_dry:
            logger.info(f"[DRY RUN] Would import {len(files_to_import)} movie file(s) to Radarr [{category.value}]")
            for f in files_to_import:
                logger.debug(f"  [DRY RUN] File: {f.get('path')} | MovieId: {f.get('movieId')}")
            return True

        payload = {
            "name": "ManualImport",
            "files": files_to_import,
            "importMode": "auto"
        }
        try:
            logger.info(f"Submitting {len(files_to_import)} movie file(s) to Radarr for import [{category.value}]...")
            res = await self._post("command", payload)
            cmd_id = res.get("id")
            logger.debug(f"Triggered Radarr ManualImport command ID: {cmd_id}")

            if cmd_id:
                # Poll command status up to 10 seconds to confirm execution
                for _ in range(5):
                    await asyncio.sleep(2)
                    try:
                        cmd_status = await self._get(f"command/{cmd_id}")
                        state = cmd_status.get("status") or cmd_status.get("state")
                        if state == "completed":
                            logger.info(f"Radarr successfully imported {len(files_to_import)} movie file(s) (Command ID: {cmd_id})")
                            return True
                        elif state == "failed":
                            logger.error(f"Radarr ManualImport command {cmd_id} failed: {cmd_status.get('message', 'Unknown failure')}")
                            return False
                    except Exception as pe:
                        logger.debug(f"Failed to poll Radarr command {cmd_id} status: {pe}")
                        break

            return True
        except Exception as e:
            logger.error(f"Failed to trigger Radarr ManualImport command: {e}")
            return False

    async def process_queue_item(self, item: Dict[str, Any], ai_provider: BaseAIProvider) -> bool:
        """
        Analyze a queued movie item. Classify the issue, check category dry_run/enabled settings,
        and use AI to verify and import.
        """
        title = item.get("title", "")
        status = (item.get("trackedDownloadStatus") or "").lower()
        tracked_state = (item.get("trackedDownloadState") or "").lower()
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        movie_info = item.get("movie")

        # Never touch downloads in normal non-blocked states:
        # downloading: active in torrent client
        # importpending: completed, queued for Radarr's built-in completed download handler to import naturally!
        # importing / imported: Radarr is actively processing the import
        # queued, delay, paused: waiting in line
        if tracked_state in ("downloading", "importing", "imported", "importpending", "queued", "delay", "paused"):
            return False

        # Output path is required for importing completed media files
        if not output_path or not output_path.strip() or output_path.strip() in (".", "/"):
            return False

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        # Ignore stalled downloads with 0 connections (handled by torrent client daemon, not an import failure)
        error_msg = (item.get("errorMessage") or "").lower()
        if "stalled" in error_msg and ("no connections" in error_msg or not all_messages):
            return False
        if any("stalled with no connections" in m.lower() for m in all_messages):
            return False

        category = classify_issue(all_messages, has_media_info=bool(movie_info))

        # If Radarr has not flagged an issue and item is not importBlocked, let Radarr complete normally
        if status not in ("warning", "error") and tracked_state != "importblocked" and not all_messages and movie_info:
            return False

        if not self.categories.is_enabled(category.value):
            logger.debug(f"Skipping '{title}': category '{category.value}' is disabled in configuration.")
            return False

        is_cat_dry_run = self.categories.is_dry_run(category.value, self.dry_run)
        mode_tag = "[DRY RUN]" if is_cat_dry_run else "[LIVE]"
        logger.info(f"Processing blocked movie: '{title}' [{category.value}] {mode_tag}")
        logger.debug(f"  Queue status: {status} (state: {tracked_state}) | Messages: {all_messages}")

        p = Path(output_path)
        folder_name = p.parent.name if p.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv") else (p.name or title)
        matched_from_library = False

        # If movie is unknown, attempt to match release to Radarr library using AI
        if not movie_info:
            logger.info(f"  Searching Radarr library to identify unknown release '{title}'...")
            all_movies = await self.get_all_movies()
            if not all_movies:
                logger.warning("  Could not retrieve library movies from Radarr.")
                return False

            candidates = [
                {
                    "id": m.get("id"),
                    "title": m.get("title"),
                    "year": m.get("year"),
                    "aliases": [a.get("title") for a in m.get("alternateTitles", [])]
                }
                for m in all_movies
            ]
            match_res = await ai_provider.match_unknown_media(
                release_title=title,
                folder_name=folder_name,
                media_type="movie",
                candidates=candidates
            )

            if not match_res.matched_id or match_res.confidence < self.min_confidence:
                logger.warning(f"  Could not match '{title}' to any movie in Radarr library ({match_res.confidence:.0%}): {match_res.reasoning}")
                return False

            movie_info = next((m for m in all_movies if m.get("id") == match_res.matched_id), None)
            if not movie_info:
                logger.warning(f"  Movie ID {match_res.matched_id} matched by AI not found in Radarr library.")
                return False

            logger.info(f"  AI matched release to library movie: '{movie_info.get('title')}' ({match_res.confidence:.0%})")
            logger.debug(f"  Library match reasoning: {match_res.reasoning}")
            matched_from_library = True

        movie_id = movie_info.get("id")
        candidate_title = movie_info.get("title", "")
        year = movie_info.get("year")
        alternate_titles = [a.get("title") for a in movie_info.get("alternateTitles", [])]

        # Movie is known (either already matched by Radarr or resolved by AI above).
        # We do not burn AI calls verifying movie identity that Radarr already confirmed.

        # Query Radarr manual import to inspect media files
        manual_items = await self.get_manual_import_items(
            folder=output_path, download_id=download_id
        )

        if not manual_items:
            logger.warning(f"  Radarr returned 0 importable files for '{title}'.")
            return False

        # Refine issue category if manual import rejections offer more specific classification
        all_manual_rejections = [r.get("reason", "") for m in manual_items for r in m.get("rejections", [])]
        if all_manual_rejections:
            refined_cat = classify_issue(all_messages, has_media_info=bool(movie_info), manual_rejections=all_manual_rejections)
            if refined_cat != category:
                logger.debug(f"  Refined issue category from '{category.value}' to '{refined_cat.value}' based on manual import rejections.")
                category = refined_cat
                if not self.categories.is_enabled(category.value):
                    logger.debug(f"Skipping '{title}': refined category '{category.value}' is disabled.")
                    return False
                is_cat_dry_run = self.categories.is_dry_run(category.value, self.dry_run)

        files_to_import: List[Dict[str, Any]] = []
        movie_path = (movie_info.get("path") or "").rstrip("/") if movie_info else ""

        for m_file in manual_items:
            file_path = m_file.get("path", "")

            # Safeguard: never import files that are already inside the destination library folder
            if movie_path and file_path.startswith(movie_path):
                logger.debug(f"Ignoring file '{file_path}' because it is inside destination library directory '{movie_path}'")
                continue

            file_name = m_file.get("name") or m_file.get("relativePath") or Path(file_path).name
            file_folder = m_file.get("folderName", "") or folder_name
            rejections = [r.get("reason", "") for r in m_file.get("rejections", [])]
            curr_movie = m_file.get("movie") or movie_info
            target_movie_id = curr_movie.get("id") if curr_movie else movie_id

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

            logger.info(f"  Approved movie file: '{file_name}' -> '{candidate_title}'")
            file_payload = {
                "path": file_path,
                "movieId": target_movie_id,
                "quality": m_file.get("quality"),
                "languages": m_file.get("languages", []),
                "releaseGroup": m_file.get("releaseGroup"),
            }
            if download_id:
                file_payload["downloadId"] = download_id
            files_to_import.append(file_payload)

        if files_to_import:
            return await self.trigger_manual_import(files_to_import, dry_run=is_cat_dry_run, category=category)

        return False
