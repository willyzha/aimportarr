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
            for k, v in kwargs.items():
                setattr(self, k, v)
    def Field(*args, **kwargs):
        if 'default_factory' in kwargs:
            return kwargs['default_factory']()
        return kwargs.get('default')
    pyd.BaseModel = BaseModel
    pyd.Field = Field
    sys.modules['pydantic'] = pyd

try:
    import httpx
except ImportError:
    sys.modules['httpx'] = MagicMock()

from src.classifier import ErrorCategory, QUALITY_REJECTION_KEYWORDS, classify_issue
from src.sonarr import SonarrClient
from src.radarr import RadarrClient


class QualityEnforcementTests(unittest.IsolatedAsyncioTestCase):
    """
    Test suite verifying that Sonarr and Radarr quality & Custom Format rules
    are strictly respected, particularly for extracted archives.
    """

    def setUp(self):
        self.sonarr = SonarrClient(
            base_url="http://mock-sonarr:8989",
            api_key="mockkey",
            min_confidence=0.8
        )
        self.radarr = RadarrClient(
            base_url="http://mock-radarr:7878",
            api_key="mockkey",
            min_confidence=0.8
        )
        self.mock_ai = MagicMock()
        self.mock_ai.parse_episode = AsyncMock()
        self.mock_ai.evaluate_sample_or_extra = AsyncMock()
        mock_series_match = MagicMock()
        mock_series_match.is_match = True
        mock_series_match.confidence = 1.0
        mock_series_match.model_name = "MockAI"
        mock_series_match.reasoning = "Series match confirmed"
        self.mock_ai.verify_series_match = AsyncMock(return_value=mock_series_match)

        mock_movie_match = MagicMock()
        mock_movie_match.is_match = True
        mock_movie_match.confidence = 1.0
        mock_movie_match.model_name = "MockAI"
        mock_movie_match.reasoning = "Movie match confirmed"
        self.mock_ai.verify_movie_match = AsyncMock(return_value=mock_movie_match)

    async def test_sonarr_extracted_archive_rejected_on_equal_or_higher_quality(self):
        """Extracted files from an archive rejected by Sonarr for equal/higher quality must not import."""
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
                {"reason": "Not a Custom Format upgrade"}
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
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch("src.sonarr.find_archive_file", return_value=archive_file), \
             patch("src.sonarr.inspect_archive", return_value=mock_inspection), \
             patch("src.sonarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 501, "seasonNumber": 51, "episodeNumber": 5}]

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            # Must not import
            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            # AI episode parser must not have been invoked to bypass Sonarr's quality rejection
            self.mock_ai.parse_episode.assert_not_called()
            # Staging directory must be cleaned up
            self.assertFalse(staging_dir.exists())

    async def test_sonarr_extracted_archive_rejected_on_custom_format(self):
        """Extracted files rejected for custom format upgrade must be skipped."""
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
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps, \
             patch("src.sonarr.find_archive_file", return_value=archive_file), \
             patch("src.sonarr.inspect_archive", return_value=mock_inspection), \
             patch("src.sonarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            self.mock_ai.parse_episode.assert_not_called()

    async def test_sonarr_extracted_archive_approved_when_quality_meets_rules(self):
        """Extracted files with no quality rejections must be successfully imported."""
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
             patch("src.sonarr.find_archive_file", return_value=archive_file), \
             patch("src.sonarr.inspect_archive", return_value=mock_inspection), \
             patch("src.sonarr.extract_archive", return_value=True):

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]
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
        """Extracted movie archive rejected by Radarr for equal or higher quality must not import."""
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
        """When curr_episodes is empty (unmapped episode) and no quality rejections, AI parse_episode is called."""
        manual_items = [{
            "path": "/downloads/Show.mkv",
            "name": "Show.mkv",
            "rejections": [],
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

        mock_match = MagicMock()
        mock_match.confidence = 0.95
        mock_match.season_number = 1
        mock_match.episode_numbers = [2]
        mock_match.model_name = "MockAI"
        mock_match.reasoning = "Parsed S01E02"
        self.mock_ai.parse_episode.return_value = mock_match

        with patch.object(self.sonarr, "get_manual_import_items", new_callable=AsyncMock) as mock_get_manual, \
             patch.object(self.sonarr, "trigger_manual_import", new_callable=AsyncMock) as mock_trigger, \
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps:

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 102, "seasonNumber": 1, "episodeNumber": 2}]
            mock_trigger.return_value = True

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertTrue(imported)
            self.mock_ai.parse_episode.assert_called_once()

    async def test_sonarr_mapped_episode_with_quality_rejection_never_calls_ai_parse(self):
        """When curr_episodes is known, but quality rule rejects, AI parse_episode must NOT be called."""
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
             patch.object(self.sonarr, "get_series_episodes", new_callable=AsyncMock) as mock_eps:

            mock_get_manual.return_value = manual_items
            mock_eps.return_value = [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]

            imported = await self.sonarr.process_queue_item(
                item=queue_item,
                ai_provider=self.mock_ai,
            )

            self.assertFalse(imported)
            mock_trigger.assert_not_called()
            self.mock_ai.parse_episode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
