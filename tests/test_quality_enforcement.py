import sys
import unittest
from datetime import datetime, timedelta, timezone
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

    async def test_classify_season_pack_release_tiers(self):
        """Verify Tier 1 (existing-file quality only), Tier 2 (Unknown Series), and hard disqualifiers."""
        # 1. Unrejected fullSeason pack -> tier1_eligible
        self.assertEqual(
            self.sonarr.classify_season_pack_release(
                {"fullSeason": True, "seasonNumber": 1, "protocol": "usenet", "rejected": False, "rejections": []},
                season_number=1,
            ),
            "tier1_eligible",
        )

        # 2. Rejected ONLY for existing file on disk higher quality + Custom Format -> tier1_eligible
        self.assertEqual(
            self.sonarr.classify_season_pack_release(
                {
                    "fullSeason": True,
                    "seasonNumber": 1,
                    "protocol": "torrent",
                    "seeders": 12,
                    "rejected": True,
                    "rejections": [
                        "Existing file on disk is of equal or higher preference: WEBDL-1080p",
                        "Not a Custom Format upgrade for existing episode file(s)",
                    ],
                },
                season_number=1,
            ),
            "tier1_eligible",
        )

        # 3. Rejected for Unknown Series + existing file quality -> tier2_unknown_series
        self.assertEqual(
            self.sonarr.classify_season_pack_release(
                {
                    "fullSeason": True,
                    "seasonNumber": 1,
                    "protocol": "torrent",
                    "seeders": 5,
                    "rejected": True,
                    "rejections": [
                        "Unknown Series",
                        "Existing file on disk is of equal or higher preference",
                    ],
                },
                season_number=1,
            ),
            "tier2_unknown_series",
        )

        # 4. Disqualified: 0 seeders on torrent
        self.assertEqual(
            self.sonarr.classify_season_pack_release(
                {
                    "fullSeason": True,
                    "seasonNumber": 1,
                    "protocol": "torrent",
                    "seeders": 0,
                    "rejected": True,
                    "rejections": ["Existing file on disk is of equal or higher preference"],
                },
                season_number=1,
            ),
            "disqualified",
        )

        # 5. Disqualified: Unwanted language or unwanted quality in profile
        self.assertEqual(
            self.sonarr.classify_season_pack_release(
                {
                    "fullSeason": True,
                    "seasonNumber": 1,
                    "protocol": "usenet",
                    "rejected": True,
                    "rejections": [
                        "Existing file on disk is of equal or higher preference",
                        "Language German is not wanted",
                    ],
                },
                season_number=1,
            ),
            "disqualified",
        )

    async def test_season_pack_backfill_end_to_end_tier1_and_tier2(self):
        """Verify Season Pack Backfill skips ongoing seasons, grabs best Tier 1 pack, and falls back to Tier 2 AI."""
        from src.ai.base import BackfillPackMatchResult

        # Series 10: Season 1 is completed (aired in 2024) with 1 existing file and 1 missing file;
        # Season 2 has an unaired/future episode (should be skipped).
        series_list = [
            {
                "id": 10,
                "title": "Test Show",
                "year": 2024,
                "monitored": True,
                "seasons": [
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": True},
                ],
            }
        ]
        episodes_s10 = [
            {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True, "airDateUtc": "2024-01-01T00:00:00Z"},
            {"id": 102, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False, "airDateUtc": "2024-01-08T00:00:00Z"},
            {"id": 201, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False, "airDateUtc": "2099-01-01T00:00:00Z"},
        ]
        releases_s1 = [
            {
                "guid": "guid-low-rank",
                "indexerId": 1,
                "title": "Test.Show.S01.720p.BluRay",
                "fullSeason": True,
                "seasonNumber": 1,
                "protocol": "usenet",
                "customFormatScore": 10,
                "qualityWeight": 4,
                "rejected": True,
                "rejections": ["Existing file on disk is of equal or higher preference"],
            },
            {
                "guid": "guid-best-rank",
                "indexerId": 2,
                "title": "Test.Show.S01.1080p.WEB-DL",
                "fullSeason": True,
                "seasonNumber": 1,
                "protocol": "usenet",
                "customFormatScore": 50,
                "qualityWeight": 6,
                "rejected": True,
                "rejections": ["Existing file on disk is of equal or higher preference"],
            },
        ]

        with patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=series_list), \
             patch.object(self.sonarr, "get_queue", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=episodes_s10), \
             patch.object(self.sonarr, "get_season_releases", new_callable=AsyncMock, return_value=releases_s1) as mock_search, \
             patch.object(self.sonarr, "grab_release", new_callable=AsyncMock, return_value=True) as mock_grab:

            grabbed = await self.sonarr.run_season_pack_backfill(ai_provider=self.mock_ai)
            self.assertEqual(grabbed, 1)
            # Only Season 1 was searched (Season 2 is still airing in 2099)
            mock_search.assert_called_once_with(10, 1)
            mock_grab.assert_called_once_with(
                guid="guid-best-rank",
                indexer_id=2,
                dry_run=False,
                title="Test.Show.S01.1080p.WEB-DL",
            )

        # Tier 2 AI Fallback test: reset cooldown memory and return only 'Unknown Series' season pack
        self.sonarr._attempted_backfill_keys.clear()
        releases_unknown = [
            {
                "guid": "guid-pinyin-pack",
                "indexerId": 3,
                "title": "CeShiJu.S01.Complete.1080p.WEB-DL",
                "fullSeason": True,
                "seasonNumber": 1,
                "protocol": "usenet",
                "customFormatScore": 25,
                "qualityWeight": 5,
                "rejected": True,
                "rejections": ["Unknown Series"],
            }
        ]
        self.mock_ai.evaluate_backfill_season_pack = AsyncMock(
            return_value=BackfillPackMatchResult(
                matched_guid="guid-pinyin-pack",
                confidence=0.94,
                reasoning="Matched Pinyin title to Test Show Season 1",
                model_name="MockAI",
            )
        )

        with patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=series_list), \
             patch.object(self.sonarr, "get_queue", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=episodes_s10), \
             patch.object(self.sonarr, "get_season_releases", new_callable=AsyncMock, return_value=releases_unknown), \
             patch.object(self.sonarr, "grab_release", new_callable=AsyncMock, return_value=True) as mock_grab2:

            grabbed2 = await self.sonarr.run_season_pack_backfill(ai_provider=self.mock_ai)
            self.assertEqual(grabbed2, 1)
            self.mock_ai.evaluate_backfill_season_pack.assert_called_once()
            mock_grab2.assert_called_once_with(
                guid="guid-pinyin-pack",
                indexer_id=3,
                dry_run=False,
                title="CeShiJu.S01.Complete.1080p.WEB-DL",
            )

    async def test_satisfied_season_pack_with_quality_rejection_dismisses_without_blocklist(self):
        """When all monitored episodes in a season now have files on disk, leftover quality-rejected pack files dismiss cleanly without blocklisting."""
        manual_items = [
            {
                "path": "/downloads/Show.S01/Show.S01E01.mkv",
                "name": "Show.S01E01.mkv",
                "rejections": [{"reason": "Existing file on disk is of equal or higher preference"}],
                "episodes": [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}],
                "series": {"id": 5, "title": "Show"},
            },
            {
                "path": "/downloads/Show.S01/Show.S01E02.mkv",
                "name": "Show.S01E02.mkv",
                "rejections": [{"reason": "Existing file on disk is of equal or higher preference"}],
                "episodes": [{"id": 102, "seasonNumber": 1, "episodeNumber": 2}],
                "series": {"id": 5, "title": "Show"},
            },
        ]
        queue_item = {
            "id": 109,
            "title": "Show.S01.720p.BluRay",
            "statusMessages": [{"messages": ["Existing file on disk is of equal or higher preference"]}],
            "series": {"id": 5, "title": "Show"},
            "outputPath": "/downloads/Show.S01",
        }
        all_eps_satisfied = [
            {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
            {"id": 102, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": True},
        ]

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock, return_value=manual_items), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=all_eps_satisfied), \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "dismiss_queue_warning", new_callable=AsyncMock) as mock_dismiss, \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve:

            await self.sonarr.process_queue_item(item=queue_item, ai_provider=self.mock_ai)
            mock_dismiss.assert_called_once()
            mock_resolve.assert_not_called()

    async def test_season_pack_already_imported_and_quality_rejected_fast_path_dismisses(self):
        """
        When Sonarr automatically imports needed episodes from a season pack ('Episode file already imported at ...')
        and leaves the remaining files rejected due to higher Custom Format score on disk, Fast-Path A must
        dismiss the leftover queue warning with 0 AI calls (removeFromClient=False, blocklist=False, skipRedownload=True).
        """
        manual_items = [
            {
                "path": "/data/downloads/Against The Current S01/Against The Current S01E01.mkv",
                "name": "Against The Current S01E01.mkv",
                "quality": {"quality": {"name": "WEBDL-1080p"}},
                "customFormatScore": 0,
                "episodes": [{"id": 23957, "seasonNumber": 1, "episodeNumber": 1}],
                "series": {"id": 343, "title": "Against The Current"},
                "rejections": [{"reason": "Episode file already imported at 10/23/2026 12:15:04"}],
            },
            {
                "path": "/data/downloads/Against The Current S01/Against The Current S01E02.mkv",
                "name": "Against The Current S01E02.mkv",
                "quality": {"quality": {"name": "WEBDL-1080p"}},
                "customFormatScore": 0,
                "episodes": [{"id": 23958, "seasonNumber": 1, "episodeNumber": 2}],
                "series": {"id": 343, "title": "Against The Current"},
                "rejections": [
                    {
                        "reason": "Not a Custom Format upgrade for existing episode file(s). New: [] (0) do not improve on Existing: [DSNP, HD Streaming Boost] (150)"
                    }
                ],
            },
        ]
        queue_item = {
            "id": 9901,
            "downloadId": "DL_ATC_S01",
            "title": "Against The Current S01 (2026) 1080p WETV WEB-DL AAC H.264-Luvmichelle",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [
                {
                    "messages": [
                        "Not a Custom Format upgrade for existing episode file(s). New: [] (0) do not improve on Existing: [DSNP, HD Streaming Boost] (150)"
                    ]
                }
            ],
            "series": {"id": 343, "title": "Against The Current", "year": 2026},
            "outputPath": "/data/downloads/Against The Current S01 (2026) 1080p WETV WEB-DL AAC H.264-Luvmichelle",
        }
        all_episodes = [
            {"id": 23957, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "monitored": True},
            {"id": 23958, "seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "monitored": True},
        ]

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock, return_value=manual_items), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=all_episodes), \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "resolve_queue_item", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = True
            await self.sonarr.process_queue_item(item=queue_item, ai_provider=self.mock_ai)

            # 0 AI calls!
            self.mock_ai.evaluate_import_decision.assert_not_called()
            # Dismissed without blocklisting or deleting from client
            mock_resolve.assert_called_once()
            self.assertFalse(mock_resolve.call_args.kwargs["remove_from_client"])
            self.assertFalse(mock_resolve.call_args.kwargs["blocklist"])
            self.assertTrue(mock_resolve.call_args.kwargs["skip_redownload"])

    async def test_season_pack_mix_already_imported_and_clean_file_fast_path_b(self):
        """
        When a season pack has a mix of 'already imported', 'Not a Custom Format upgrade', and 1 clean
        unimported episode, Fast-Path B imports only the 1 clean episode with 0 AI calls and dismisses the warning.
        """
        manual_items = [
            {
                "path": "/data/downloads/Pack/S01E01.mkv",
                "name": "S01E01.mkv",
                "quality": {"quality": {"name": "WEBDL-1080p"}},
                "episodes": [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}],
                "series": {"id": 343, "title": "Show"},
                "rejections": [{"reason": "Episode file already imported at 10/23/2026 12:15:04"}],
            },
            {
                "path": "/data/downloads/Pack/S01E02.mkv",
                "name": "S01E02.mkv",
                "quality": {"quality": {"name": "WEBDL-1080p"}},
                "episodes": [{"id": 102, "seasonNumber": 1, "episodeNumber": 2}],
                "series": {"id": 343, "title": "Show"},
                "rejections": [{"reason": "Not a Custom Format upgrade for existing episode file(s)"}],
            },
            {
                "path": "/data/downloads/Pack/S01E03.mkv",
                "name": "S01E03.mkv",
                "quality": {"quality": {"name": "WEBDL-1080p"}},
                "episodes": [{"id": 103, "seasonNumber": 1, "episodeNumber": 3}],
                "series": {"id": 343, "title": "Show"},
                "rejections": [],
            },
        ]
        queue_item = {
            "id": 9902,
            "downloadId": "DL_PACK_MIX",
            "title": "Show S01 1080p WEB-DL",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"messages": ["Not a Custom Format upgrade for existing episode file(s)"]}],
            "series": {"id": 343, "title": "Show", "year": 2026},
            "outputPath": "/data/downloads/Pack",
        }

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock, return_value=manual_items), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "get_episode_files", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock, return_value=True) as mock_import, \
             patch.object(self.sonarr, "dismiss_queue_warning", new_callable=AsyncMock, return_value=True) as mock_dismiss:
            res = await self.sonarr.process_queue_item(item=queue_item, ai_provider=self.mock_ai)

            self.assertTrue(res)
            self.mock_ai.evaluate_import_decision.assert_not_called()
            mock_import.assert_called_once()
            imported_files = mock_import.call_args.args[0]
            self.assertEqual(len(imported_files), 1)
            self.assertEqual(imported_files[0]["path"], "/data/downloads/Pack/S01E03.mkv")
            mock_dismiss.assert_called_once()

    async def test_stage3_output_interpreter_repairs_malformed_ollama_response(self):
        """
        Verify that when Stage 2 returns a malformed JSON object (e.g. 'action' instead of 'decision',
        missing 'confidence', episode ID in 'matched_media_id', and string in 'files_to_import'),
        Stage 3 AI Output Interpreter is invoked to repair it into a valid Stage2DecisionResult.
        """
        from src.ai.ollama import OllamaProvider

        provider = OllamaProvider(base_url="http://localhost:11434", model="qwen2.5:1.5b")

        # First call (Stage 2) returns the exact malformed JSON from the user's trace
        malformed_stage2 = {
            "action": "import",
            "matched_media_id": 23957,
            "files_to_import": ["Episode 1"],
        }
        # Second call (Stage 3 Output Interpreter) repairs it using the Reference Context
        repaired_stage3 = {
            "decision": "import",
            "matched_media_id": 343,
            "files_to_import": [
                {
                    "path": "/data/downloads/Against The Current S01/Against The Current S01E01.mkv",
                    "season_number": 1,
                    "episode_numbers": [1],
                }
            ],
            "category_tag": "episode_parsed",
            "confidence": 0.92,
            "reasoning": "Repaired malformed Stage 2 response: matched series 343 and mapped Episode 1 path.",
        }

        with patch.object(
            provider,
            "_call_ollama_with_raw",
            new_callable=AsyncMock,
            return_value=(malformed_stage2, '{"action": "import", "matched_media_id": 23957, "files_to_import": ["Episode 1"]}', None),
        ) as mock_stage2_call, patch.object(
            provider,
            "_call_ollama",
            new_callable=AsyncMock,
            return_value=repaired_stage3,
        ) as mock_stage3_call:
            result = await provider.evaluate_import_decision(
                media_type="series",
                release_title="Against The Current S01",
                folder_name="Against The Current S01",
                queue_messages=["Episode was not found"],
                candidate_media={"id": 343, "title": "Against The Current", "year": 2026},
                library_candidates=[],
                manual_import_files=[
                    {
                        "path": "/data/downloads/Against The Current S01/Against The Current S01E01.mkv",
                        "name": "Against The Current S01E01.mkv",
                        "episodes": [{"id": 23957, "season": 1, "episode": 1}],
                    }
                ],
                existing_disk_files=[],
                known_episodes=[
                    {"id": i, "season": 1, "episode": i, "title": f"Episode {i}", "hasFile": True}
                    for i in range(1, 48)
                ],
            )

            mock_stage2_call.assert_called_once()
            mock_stage3_call.assert_called_once()
            self.assertEqual(mock_stage3_call.call_args.kwargs["operation"], "stage3_output_interpreter")
            self.assertEqual(result.decision, "import")
            self.assertEqual(result.matched_media_id, 343)
            self.assertEqual(result.confidence, 0.92)
            self.assertEqual(len(result.files_to_import), 1)
            self.assertEqual(
                result.files_to_import[0].path,
                "/data/downloads/Against The Current S01/Against The Current S01E01.mkv",
            )

    async def test_classify_episode_release_tiers(self):
        """Verify episode release classification into tier1_eligible, tier2_unknown_series, and disqualified."""
        # 1. Unrejected matching episode release -> tier1_eligible
        self.assertEqual(
            self.sonarr.classify_episode_release(
                {
                    "fullSeason": False,
                    "seasonNumber": 2,
                    "episodeNumbers": [5],
                    "protocol": "usenet",
                    "rejected": False,
                    "rejections": [],
                },
                season_number=2,
                episode_number=5,
            ),
            "tier1_eligible",
        )

        # 2. Unknown Series episode release -> tier2_unknown_series
        self.assertEqual(
            self.sonarr.classify_episode_release(
                {
                    "fullSeason": False,
                    "seasonNumber": 2,
                    "episodeNumbers": [5],
                    "protocol": "torrent",
                    "seeders": 8,
                    "rejected": True,
                    "rejections": ["Unknown Series"],
                },
                season_number=2,
                episode_number=5,
            ),
            "tier2_unknown_series",
        )

        # 3. Full season pack -> disqualified in single-episode search
        self.assertEqual(
            self.sonarr.classify_episode_release(
                {
                    "fullSeason": True,
                    "seasonNumber": 2,
                    "episodeNumbers": [],
                    "protocol": "usenet",
                    "rejected": False,
                    "rejections": [],
                },
                season_number=2,
                episode_number=5,
            ),
            "disqualified",
        )

        # 4. Wrong episode number -> disqualified
        self.assertEqual(
            self.sonarr.classify_episode_release(
                {
                    "fullSeason": False,
                    "seasonNumber": 2,
                    "episodeNumbers": [6],
                    "protocol": "usenet",
                    "rejected": False,
                    "rejections": [],
                },
                season_number=2,
                episode_number=5,
            ),
            "disqualified",
        )

    async def test_missing_episode_retry_ongoing_season_tier1_and_tier2(self):
        """
        Verify Missing Episode Retry skips completed seasons, skips newly-aired episodes within
        the 6h post-air delay, grabs the best Tier 1 release for missing aired episodes in ongoing
        seasons, and falls back to Tier 2 AI for 'Unknown Series' episode releases.
        """
        from datetime import datetime, timedelta, timezone
        from src.ai.base import BackfillPackMatchResult

        now_utc = datetime.now(timezone.utc)
        aired_2_days_ago = (now_utc - timedelta(days=2)).isoformat()
        aired_1_hour_ago = (now_utc - timedelta(hours=1)).isoformat()
        airs_next_week = (now_utc + timedelta(days=7)).isoformat()

        series_list = [
            {
                "id": 20,
                "title": "Ongoing Show",
                "year": 2026,
                "monitored": True,
                "seasons": [
                    {"seasonNumber": 1, "monitored": True},  # Completed season in 2024
                    {"seasonNumber": 2, "monitored": True},  # Ongoing season
                ],
            }
        ]
        episodes_s20 = [
            # Season 1: Completed in 2024 -> should be ignored by run_missing_episode_retry
            {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False, "airDateUtc": "2024-01-01T00:00:00Z"},
            # Season 2: Ongoing season
            {"id": 201, "seasonNumber": 2, "episodeNumber": 1, "title": "Ep 1", "monitored": True, "hasFile": True, "airDateUtc": aired_2_days_ago},
            {"id": 202, "seasonNumber": 2, "episodeNumber": 2, "title": "Ep 2", "monitored": True, "hasFile": False, "airDateUtc": aired_2_days_ago},
            {"id": 203, "seasonNumber": 2, "episodeNumber": 3, "title": "Ep 3", "monitored": True, "hasFile": False, "airDateUtc": aired_1_hour_ago},
            {"id": 204, "seasonNumber": 2, "episodeNumber": 4, "title": "Ep 4", "monitored": True, "hasFile": False, "airDateUtc": airs_next_week},
        ]
        releases_ep202 = [
            {
                "guid": "ep-guid-720p",
                "indexerId": 1,
                "title": "Ongoing.Show.S02E02.720p.WEB-DL",
                "fullSeason": False,
                "seasonNumber": 2,
                "episodeNumbers": [2],
                "protocol": "usenet",
                "customFormatScore": 10,
                "qualityWeight": 4,
                "rejected": False,
                "rejections": [],
            },
            {
                "guid": "ep-guid-1080p",
                "indexerId": 2,
                "title": "Ongoing.Show.S02E02.1080p.WEB-DL",
                "fullSeason": False,
                "seasonNumber": 2,
                "episodeNumbers": [2],
                "protocol": "usenet",
                "customFormatScore": 80,
                "qualityWeight": 6,
                "rejected": False,
                "rejections": [],
            },
        ]

        with patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=series_list), \
             patch.object(self.sonarr, "get_queue", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=episodes_s20), \
             patch.object(self.sonarr, "get_episode_releases", new_callable=AsyncMock, return_value=releases_ep202) as mock_ep_search, \
             patch.object(self.sonarr, "grab_release", new_callable=AsyncMock, return_value=True) as mock_grab:

            grabbed = await self.sonarr.run_missing_episode_retry(ai_provider=self.mock_ai)
            self.assertEqual(grabbed, 1)
            # Only Episode 202 (S02E02) was searched (S01 is completed; S02E01 hasFile; S02E03 aired 1h ago < 6h delay; S02E04 airs next week)
            mock_ep_search.assert_called_once_with(202)
            mock_grab.assert_called_once_with(
                guid="ep-guid-1080p",
                indexer_id=2,
                dry_run=False,
                title="Ongoing.Show.S02E02.1080p.WEB-DL",
            )

        # Tier 2 AI Fallback test for 'Unknown Series' episode release
        self.sonarr._attempted_episode_retry_keys.clear()
        releases_ep202_unknown = [
            {
                "guid": "ep-guid-pinyin",
                "indexerId": 3,
                "title": "LianZaiJu.S02E02.1080p.WEB-DL",
                "fullSeason": False,
                "seasonNumber": 2,
                "episodeNumbers": [2],
                "protocol": "usenet",
                "customFormatScore": 40,
                "qualityWeight": 6,
                "rejected": True,
                "rejections": ["Unknown Series"],
            }
        ]
        self.mock_ai.evaluate_backfill_episode = AsyncMock(
            return_value=BackfillPackMatchResult(
                matched_guid="ep-guid-pinyin",
                confidence=0.95,
                reasoning="Matched Pinyin title to Ongoing Show S02E02",
                model_name="MockAI",
            )
        )

        with patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=series_list), \
             patch.object(self.sonarr, "get_queue", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=episodes_s20), \
             patch.object(self.sonarr, "get_episode_releases", new_callable=AsyncMock, return_value=releases_ep202_unknown), \
             patch.object(self.sonarr, "grab_release", new_callable=AsyncMock, return_value=True) as mock_grab2:

            grabbed2 = await self.sonarr.run_missing_episode_retry(ai_provider=self.mock_ai)
            self.assertEqual(grabbed2, 1)
            self.mock_ai.evaluate_backfill_episode.assert_called_once()
            mock_grab2.assert_called_once_with(
                guid="ep-guid-pinyin",
                indexer_id=3,
                dry_run=False,
                title="LianZaiJu.S02E02.1080p.WEB-DL",
            )

    def test_classify_episode_release_tiers_and_rejections(self):
        """Test classifying episode releases: Tier 1, Tier 2, and hard disqualifiers."""
        # Single-episode release matching target
        rel_single = {
            "seasonNumber": 1,
            "episodeNumbers": [2],
            "rejected": False,
            "rejections": [],
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_single, season_number=1, episode_number=2), "tier1_eligible")
        # Wrong episode number
        self.assertEqual(self.sonarr.classify_episode_release(rel_single, season_number=1, episode_number=3), "disqualified")

        # Multi-episode release matching target
        rel_multi = {
            "seasonNumber": 1,
            "episodeNumbers": [2, 3],
            "rejected": False,
            "rejections": [],
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_multi, season_number=1, episode_number=2), "tier1_eligible")
        self.assertEqual(self.sonarr.classify_episode_release(rel_multi, season_number=1, episode_number=3), "tier1_eligible")

        # Multi-episode release rejected ONLY for existing quality on companion episode
        rel_multi_quality_rejected = {
            "seasonNumber": 1,
            "episodeNumbers": [1, 2],
            "rejected": True,
            "rejections": ["Not a Custom Format upgrade for existing episode file(s). New: [] (0) do not improve on Existing: [DSNP] (100)"],
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_multi_quality_rejected, season_number=1, episode_number=2), "tier1_eligible")

        # Unknown Series rejection -> Tier 2
        rel_unknown = {
            "seasonNumber": 1,
            "episodeNumbers": [2],
            "rejected": True,
            "rejections": ["Unknown Series"],
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_unknown, season_number=1, episode_number=2), "tier2_unknown_series")

        # Full season release -> Disqualified for episode search
        rel_full_season = {
            "seasonNumber": 1,
            "episodeNumbers": [2],
            "fullSeason": True,
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_full_season, season_number=1, episode_number=2), "disqualified")

        # Torrent with 0 seeders -> Disqualified
        rel_zero_seeders = {
            "seasonNumber": 1,
            "episodeNumbers": [2],
            "protocol": "torrent",
            "seeders": 0,
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_zero_seeders, season_number=1, episode_number=2), "disqualified")

        # Hard disqualifier (blacklist / blocklist)
        rel_blacklisted = {
            "seasonNumber": 1,
            "episodeNumbers": [2],
            "rejected": True,
            "rejections": ["Release contains blacklisted word: sample"],
        }
        self.assertEqual(self.sonarr.classify_episode_release(rel_blacklisted, season_number=1, episode_number=2), "disqualified")

    def test_ongoing_season_detection_air_date_and_status(self):
        """Test ongoing season detection with series_status and air dates."""
        now_utc = datetime.now(timezone.utc)
        aired_long_ago = (now_utc - timedelta(days=30)).isoformat()
        airs_in_future = (now_utc + timedelta(days=5)).isoformat()

        season_eps_all_aired = [
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True, "airDateUtc": aired_long_ago},
            {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False, "airDateUtc": aired_long_ago},
        ]

        # 1. Series ended and all episodes aired > grace days -> not ongoing, returns []
        res_ended = self.sonarr.find_eligible_missing_episodes_for_ongoing_season(
            season_eps_all_aired,
            is_latest_season=True,
            series_status="ended",
        )
        self.assertEqual(res_ended, [])

        # 2. Series continuing and is latest season -> ongoing! Returns missing ep 2
        res_continuing = self.sonarr.find_eligible_missing_episodes_for_ongoing_season(
            season_eps_all_aired,
            is_latest_season=True,
            series_status="continuing",
        )
        self.assertEqual(len(res_continuing), 1)
        self.assertEqual(res_continuing[0]["id"], 2)

        # 3. Series ended, but has future/TBA episode -> ongoing!
        season_eps_with_future = [
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False, "airDateUtc": aired_long_ago},
            {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False, "airDateUtc": airs_in_future},
        ]
        res_future = self.sonarr.find_eligible_missing_episodes_for_ongoing_season(
            season_eps_with_future,
            is_latest_season=False,
            series_status="ended",
        )
        self.assertEqual(len(res_future), 1)
        self.assertEqual(res_future[0]["id"], 1)

    async def test_multi_episode_companion_satisfaction(self):
        """Verify grabbing a multi-episode release satisfies and records all covered episodes."""
        series = {"id": 10, "title": "Test Show", "year": 2026}
        missing_eps = [
            {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "title": "Ep 1", "monitored": True, "hasFile": False},
            {"id": 102, "seasonNumber": 1, "episodeNumber": 2, "title": "Ep 2", "monitored": True, "hasFile": False},
        ]
        # Interactive search returns a 2-in-1 release covering E01 and E02
        releases = [
            {
                "guid": "rel-multi-e01-e02",
                "indexerId": 5,
                "title": "Test.Show.S01E01-E02.1080p",
                "seasonNumber": 1,
                "episodeNumbers": [1, 2],
                "protocol": "usenet",
                "rejected": False,
                "rejections": [],
            }
        ]

        with patch.object(self.sonarr, "get_episode_releases", new_callable=AsyncMock, return_value=releases) as mock_search, \
             patch.object(self.sonarr, "grab_release", new_callable=AsyncMock, return_value=True) as mock_grab:

            queued_ep_ids = set()
            searched, grabbed = await self.sonarr._retry_missing_episodes_batch(
                series=series,
                season_num=1,
                missing_eps=missing_eps,
                queued_episode_ids=queued_ep_ids,
                max_episodes=5,
                delay_seconds=0,
            )

            # E01 was searched and grabbed S01E01-E02
            self.assertEqual(searched, 1)
            self.assertEqual(grabbed, 1)
            # E02 should be satisfied immediately without an extra interactive search
            mock_search.assert_called_once_with(101)
            mock_grab.assert_called_once()
            self.assertIn(101, queued_ep_ids)
            self.assertIn(102, queued_ep_ids)
            self.assertIn("ep_retry:sonarr:10:s01e01", self.sonarr._attempted_episode_retry_keys)
            self.assertIn("ep_retry:sonarr:10:s01e02", self.sonarr._attempted_episode_retry_keys)

    async def test_season_pack_backfill_inline_fallback_to_episodes(self):
        """Verify Season Pack Backfill falls back inline to individual episode search when no pack found."""
        series_list = [
            {
                "id": 30,
                "title": "Finished Show",
                "year": 2024,
                "monitored": True,
                "status": "ended",
                "seasons": [{"seasonNumber": 1, "monitored": True}],
            }
        ]
        season_eps = [
            {"id": 301, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True, "airDateUtc": "2024-01-01T00:00:00Z"},
            {"id": 302, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False, "airDateUtc": "2024-01-08T00:00:00Z"},
        ]

        with patch.object(self.sonarr, "get_all_series", new_callable=AsyncMock, return_value=series_list), \
             patch.object(self.sonarr, "get_queue", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock, return_value=season_eps), \
             patch.object(self.sonarr, "get_season_releases", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.sonarr, "_retry_missing_episodes_batch", new_callable=AsyncMock, return_value=(1, 1)) as mock_fallback_batch:

            packs_grabbed = await self.sonarr.run_season_pack_backfill(ai_provider=self.mock_ai)
            self.assertEqual(packs_grabbed, 0)
            mock_fallback_batch.assert_called_once()
            call_kwargs = mock_fallback_batch.call_args.kwargs
            self.assertEqual(call_kwargs["season_num"], 1)
            self.assertEqual(len(call_kwargs["missing_eps"]), 1)
            self.assertEqual(call_kwargs["missing_eps"][0]["id"], 302)


if __name__ == "__main__":
    unittest.main()
