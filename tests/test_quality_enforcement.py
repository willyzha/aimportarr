import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

# Ensure dependencies mocked if not installed in local environment
try:
    import pydantic
except ImportError:
    pyd = MagicMock()
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in list(getattr(self.__class__, "__dict__", {}).items()):
                if not k.startswith("_") and not callable(v):
                    setattr(self, k, v)
            for k, v in kwargs.items():
                setattr(self, k, v)
    def Field(*args, **kwargs):
        if "default_factory" in kwargs:
            return kwargs["default_factory"]()
        if "default" in kwargs:
            return kwargs["default"]
        return args[0] if args else None
    pyd.BaseModel = BaseModel
    pyd.Field = Field
    sys.modules["pydantic"] = pyd

try:
    import httpx
except ImportError:
    sys.modules["httpx"] = MagicMock()

from src.ai.base import Stage1TriageResult, Stage2DecisionResult, ApprovedFileItem
from src.sonarr import SonarrClient
from src.radarr import RadarrClient
from src.metrics import metrics


class QualityEnforcementTests(unittest.IsolatedAsyncioTestCase):
    """
    Test suite verifying the 4-step / two-stage AI evaluation architecture,
    quality & Custom Format enforcement, client file preservation on downgrades,
    and client deletion on malware/corrupt/sample-only releases.
    """

    def setUp(self):
        # Reset in-memory attempted items between tests
        metrics.db = None
        self.sonarr = SonarrClient(
            base_url="http://mock-sonarr:8989",
            api_key="mockkey",
            min_confidence=0.8,
            dry_run=False,
            reject_on_low_confidence=False,
        )
        self.radarr = RadarrClient(
            base_url="http://mock-radarr:7878",
            api_key="mockkey",
            min_confidence=0.8,
            dry_run=False,
            reject_on_low_confidence=False,
        )
        self.mock_ai = MagicMock()
        self.mock_ai.evaluate_stage1_triage = AsyncMock()
        self.mock_ai.evaluate_import_decision = AsyncMock()

    async def test_sonarr_extracted_archive_rejected_on_equal_or_higher_quality(self):
        """Extracted files rejected for equal/higher quality must NOT import and MUST keep files in client while blocklisting."""
        tmp_base = Path("/tmp/test_sonarr_archive_rej")
        tmp_base.mkdir(parents=True, exist_ok=True)
        archive_file = tmp_base / "mock.rar"
        archive_file.touch()
        staging_dir = tmp_base / f"mock{self.sonarr.extraction_config.staging_suffix}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        manual_items = [{
            "path": str(staging_dir / "Survivor.S51E05.HDTV.mkv"),
            "name": "Survivor.S51E05.HDTV.mkv",
            "folderName": "Survivor.S51E05.HDTV",
            "rejections": [
                {"reason": "Existing file on disk has equal or higher quality"},
                {"reason": "Not a Custom Format upgrade"},
            ],
            "episodes": [{"id": 501, "episodeNumber": 5, "seasonNumber": 51}],
            "series": {"id": 10, "title": "Survivor", "path": "/data/media/Survivor"},
            "quality": {"quality": {"id": 1, "name": "HDTV-720p"}},
        }]

        queue_item = {
            "id": 99,
            "title": "Survivor.S51E05.1080p.WEB.H264-HOTDOGWATER",
            "status": "warning",
            "trackedDownloadStatus": "warning",
            "statusMessages": [{"messages": ["No files found are eligible for import"]}],
            "series": {"id": 10, "title": "Survivor", "path": "/data/media/Survivor"},
            "outputPath": str(tmp_base),
        }

        mock_inspection = MagicMock()
        mock_inspection.has_video_files = True
        mock_inspection.has_suspicious_files = False
        mock_inspection.video_files = ["video.mkv"]

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve, \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock) as mock_ef, \
             patch("src.sonarr.find_archive_file", return_value=archive_file), \
             patch("src.sonarr.inspect_archive", return_value=mock_inspection), \
             patch("src.sonarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 501, "seasonNumber": 51, "episodeNumber": 5}]
            mock_ef.return_value = []
            mock_resolve.return_value = True

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            self.mock_ai.evaluate_import_decision.assert_not_called()
            self.assertFalse(staging_dir.exists())
            # Must dismiss with remove_from_client=False (keep in client), blocklist=True, skip_redownload=False
            mock_resolve.assert_called_once()
            self.assertFalse(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertTrue(mock_resolve.call_args.kwargs["blocklist"])
            self.assertFalse(mock_resolve.call_args.kwargs["skip_redownload"])

    async def test_sonarr_extracted_archive_rejected_on_custom_format(self):
        """Extracted files rejected for custom format upgrade must be skipped and keep files in client."""
        tmp_base = Path("/tmp/test_sonarr_cf_rej")
        tmp_base.mkdir(parents=True, exist_ok=True)
        archive_file = tmp_base / "mock.rar"
        archive_file.touch()
        staging_dir = tmp_base / f"mock{self.sonarr.extraction_config.staging_suffix}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        manual_items = [{
            "path": str(staging_dir / "Show.S01E01.mkv"),
            "name": "Show.S01E01.mkv",
            "rejections": [{"reason": "Not a Custom Format upgrade"}],
            "episodes": [{"id": 101, "episodeNumber": 1, "seasonNumber": 1}],
            "series": {"id": 5, "title": "Show"},
        }]

        queue_item = {
            "id": 100,
            "title": "Show.S01E01.RAR",
            "statusMessages": [{"messages": ["No files found to import"]}],
            "series": {"id": 5, "title": "Show"},
            "outputPath": str(tmp_base),
        }

        mock_inspection = MagicMock()
        mock_inspection.has_video_files = True
        mock_inspection.has_suspicious_files = False
        mock_inspection.video_files = ["video.mkv"]

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve, \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock) as mock_ef, \
             patch("src.sonarr.find_archive_file", return_value=archive_file), \
             patch("src.sonarr.inspect_archive", return_value=mock_inspection), \
             patch("src.sonarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]
            mock_ef.return_value = []

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            self.mock_ai.evaluate_import_decision.assert_not_called()
            mock_resolve.assert_called_once()
            self.assertFalse(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertTrue(mock_resolve.call_args.kwargs["blocklist"])

    async def test_sonarr_extracted_archive_approved_when_quality_meets_rules(self):
        """Extracted files with no quality rejections must be imported via fast-path without calling AI."""
        tmp_base = Path("/tmp/test_sonarr_ok")
        tmp_base.mkdir(parents=True, exist_ok=True)
        archive_file = tmp_base / "mock.rar"
        archive_file.touch()
        staging_dir = tmp_base / f"mock{self.sonarr.extraction_config.staging_suffix}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        manual_items = [{
            "path": str(staging_dir / "Show.S01E01.mkv"),
            "name": "Show.S01E01.mkv",
            "rejections": [],
            "episodes": [{"id": 101, "episodeNumber": 1, "seasonNumber": 1}],
            "series": {"id": 5, "title": "Show"},
            "quality": {"quality": {"id": 7, "name": "Bluray-1080p"}},
        }]

        queue_item = {
            "id": 101,
            "title": "Show.S01E01.RAR",
            "statusMessages": [{"messages": ["No files found to import"]}],
            "series": {"id": 5, "title": "Show"},
            "outputPath": str(tmp_base),
        }

        mock_inspection = MagicMock()
        mock_inspection.has_video_files = True
        mock_inspection.has_suspicious_files = False
        mock_inspection.video_files = ["video.mkv"]

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock) as mock_ef, \
             patch("src.sonarr.find_archive_file", return_value=archive_file), \
             patch("src.sonarr.inspect_archive", return_value=mock_inspection), \
             patch("src.sonarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]
            mock_ef.return_value = []
            mock_trigger.return_value = True

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertTrue(imported)
            mock_trigger.assert_called_once()
            called_payload = mock_trigger.call_args[0][0]
            self.assertEqual(len(called_payload), 1)
            self.assertEqual(called_payload[0]["episodeIds"], [101])

    async def test_radarr_extracted_archive_rejected_on_higher_quality(self):
        """Extracted movie archive rejected by Radarr for equal/higher quality must keep files in client and blocklist."""
        tmp_base = Path("/tmp/test_radarr_archive_rej")
        tmp_base.mkdir(parents=True, exist_ok=True)
        archive_file = tmp_base / "mock.rar"
        archive_file.touch()
        staging_dir = tmp_base / f"mock{self.radarr.extraction_config.staging_suffix}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        manual_items = [{
            "path": str(staging_dir / "Movie.2026.mkv"),
            "name": "Movie.2026.mkv",
            "rejections": [{"reason": "Existing file on disk has equal or higher quality"}],
            "movie": {"id": 55, "title": "Movie (2026)"},
        }]

        queue_item = {
            "id": 200,
            "title": "Movie.2026.RAR",
            "statusMessages": [{"messages": ["No files found to import"]}],
            "movie": {"id": 55, "title": "Movie (2026)"},
            "outputPath": str(tmp_base),
        }

        mock_inspection = MagicMock()
        mock_inspection.has_video_files = True
        mock_inspection.has_suspicious_files = False
        mock_inspection.video_files = ["video.mkv"]

        with patch.object(self.radarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.radarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.radarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve, \
             patch("src.radarr.find_archive_file", return_value=archive_file), \
             patch("src.radarr.inspect_archive", return_value=mock_inspection), \
             patch("src.radarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items

            imported = await self.radarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            self.assertFalse(staging_dir.exists())
            mock_resolve.assert_called_once()
            self.assertFalse(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertTrue(mock_resolve.call_args.kwargs["blocklist"])

    async def test_radarr_extracted_archive_approved_when_valid(self):
        """Extracted movie archive with no rejections is imported cleanly."""
        tmp_base = Path("/tmp/test_radarr_ok")
        tmp_base.mkdir(parents=True, exist_ok=True)
        archive_file = tmp_base / "mock.rar"
        archive_file.touch()
        staging_dir = tmp_base / f"mock{self.radarr.extraction_config.staging_suffix}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        manual_items = [{
            "path": str(staging_dir / "Movie.2026.mkv"),
            "name": "Movie.2026.mkv",
            "rejections": [],
            "movie": {"id": 55, "title": "Movie (2026)"},
        }]

        queue_item = {
            "id": 201,
            "title": "Movie.2026.RAR",
            "statusMessages": [{"messages": ["No files found to import"]}],
            "movie": {"id": 55, "title": "Movie (2026)"},
            "outputPath": str(tmp_base),
        }

        mock_inspection = MagicMock()
        mock_inspection.has_video_files = True
        mock_inspection.has_suspicious_files = False
        mock_inspection.video_files = ["video.mkv"]

        with patch.object(self.radarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.radarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch("src.radarr.find_archive_file", return_value=archive_file), \
             patch("src.radarr.inspect_archive", return_value=mock_inspection), \
             patch("src.radarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_trigger.return_value = True

            imported = await self.radarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertTrue(imported)
            mock_trigger.assert_called_once()

    async def test_sonarr_unmapped_episode_triggers_ai_parsing(self):
        """When curr_episodes is empty (unmapped episode) and no quality rejections, Stage 2 AI resolves it."""
        manual_items = [{
            "path": "/downloads/Show.mkv",
            "name": "Show.mkv",
            "rejections": [{"reason": "Unknown episode"}],
            "episodes": [],
            "series": {"id": 5, "title": "Show"},
        }]

        queue_item = {
            "id": 102,
            "title": "Show.Release",
            "statusMessages": [{"messages": ["Episode was not found"]}],
            "series": {"id": 5, "title": "Show"},
            "outputPath": "/downloads/Show.Release",
        }

        self.mock_ai.evaluate_import_decision.return_value = Stage2DecisionResult(
            decision="import",
            matched_media_id=5,
            files_to_import=[ApprovedFileItem(path="/downloads/Show.mkv", season_number=1, episode_numbers=[2])],
            category_tag="episode_parsed",
            confidence=0.95,
            reasoning="Parsed S01E02",
            model_name="MockAI",
        )

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock) as mock_ef:

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 102, "seasonNumber": 1, "episodeNumber": 2}]
            mock_ef.return_value = []
            mock_trigger.return_value = True

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertTrue(imported)
            self.mock_ai.evaluate_import_decision.assert_called_once()

    async def test_sonarr_mapped_episode_with_quality_rejection_never_calls_ai_parse(self):
        """When curr_episodes is known, but quality rule rejects, AI must NOT be called and files must be kept in client."""
        manual_items = [{
            "path": "/downloads/Show.S01E01.mkv",
            "name": "Show.S01E01.mkv",
            "rejections": [{"reason": "Existing file on disk has equal or higher quality"}],
            "episodes": [{"id": 101, "episodeNumber": 1, "seasonNumber": 1}],
            "series": {"id": 5, "title": "Show"},
        }]

        queue_item = {
            "id": 103,
            "title": "Show.S01E01",
            "statusMessages": [{"messages": ["Matched to series by id"]}],
            "series": {"id": 5, "title": "Show"},
            "outputPath": "/downloads/Show.S01E01",
        }

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve, \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock) as mock_ef:

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]
            mock_ef.return_value = []

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            self.mock_ai.evaluate_import_decision.assert_not_called()
            mock_resolve.assert_called_once()
            self.assertFalse(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertTrue(mock_resolve.call_args.kwargs["blocklist"])

    async def test_malware_release_removes_from_client_and_blocklists(self):
        """Confirmed malware/corrupt/sample-only release must set removeFromClient=True and blocklist=True."""
        queue_item = {
            "id": 104,
            "title": "Show.S01E01.7z",
            "statusMessages": [{"messages": ["Unsupported extension: '.7z'"]}],
            "series": {"id": 5, "title": "Show"},
            "outputPath": "/downloads/Show.S01E01.7z",
        }

        self.mock_ai.evaluate_stage1_triage.return_value = Stage1TriageResult(
            action="reject_delete",
            category_tag="malware_or_corrupt",
            confidence=0.98,
            reasoning="Contains executable disguised as episode",
            model_name="MockAI",
        )

        with patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = True
            await self.sonarr.process_queue_item(item=queue_item, ai_provider=self.mock_ai)

            mock_resolve.assert_called_once()
            self.assertTrue(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertTrue(mock_resolve.call_args.kwargs["blocklist"])
            self.assertFalse(mock_resolve.call_args.kwargs["skip_redownload"])

    async def test_low_confidence_conservative_vs_hands_off(self):
        """Verify reject_on_low_confidence=False leaves item in queue, while True blocklists & keeps files in client."""
        manual_items = [{
            "path": "/downloads/Ambiguous.mkv",
            "name": "Ambiguous.mkv",
            "rejections": [{"reason": "Unknown series"}],
            "episodes": [],
            "series": None,
        }]
        queue_item = {
            "id": 105,
            "title": "Ambiguous.Release",
            "statusMessages": [{"messages": ["Unknown series"]}],
            "series": None,
            "outputPath": "/downloads/Ambiguous.Release",
        }

        self.mock_ai.evaluate_import_decision = AsyncMock(
            return_value=Stage2DecisionResult(
                decision="import",
                matched_media_id=5,
                files_to_import=[ApprovedFileItem(path="/downloads/Ambiguous.mkv", season_number=1, episode_numbers=[1])],
                category_tag="unknown_media",
                confidence=0.55,  # Below min_confidence=0.80
                reasoning="Uncertain title match",
                model_name="MockAI",
            )
        )

        # 1. Conservative mode (reject_on_low_confidence=False): must NOT remove from queue
        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock, return_value=manual_items), \
             patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=[{"id": 5, "title": "Show"}]), \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve:
            await self.sonarr.process_queue_item(item=queue_item, ai_provider=self.mock_ai)
            mock_resolve.assert_not_called()

        # 2. Hands-off mode (reject_on_low_confidence=True): must dismiss (keep in client), blocklist, and re-search
        self.sonarr.reject_on_low_confidence = True
        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock, return_value=manual_items), \
             patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=[{"id": 5, "title": "Show"}]), \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve:
            await self.sonarr.process_queue_item(item=queue_item, ai_provider=self.mock_ai)
            mock_resolve.assert_called_once()
            self.assertFalse(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertTrue(mock_resolve.call_args.kwargs["blocklist"])
            self.assertFalse(mock_resolve.call_args.kwargs["skip_redownload"])


if __name__ == "__main__":
    unittest.main()
