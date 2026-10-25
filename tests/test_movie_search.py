import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from src.config import (
    AppConfig,
    MissingMovieSearchConfig,
    config_to_dict,
    update_config_from_dict,
)
from src.radarr import RadarrClient
from src.ai.base import BackfillPackMatchResult
from src.main import automation_worker_loop
from src.metrics import metrics


class MissingMovieSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        metrics.db = None
        self.mock_ai = MagicMock()
        self.client = RadarrClient(
            base_url="http://localhost:7878",
            api_key="test_api_key",
            missing_movie_config=MissingMovieSearchConfig(
                enabled=True,
                cron="0 3 * * *",
                min_release_delay_days=1,
                max_movies_per_run=5,
                movie_cooldown_days=7,
            ),
        )

    def test_config_parsing_and_serialization(self):
        cfg = AppConfig()
        cfg.radarr.enabled = True
        cfg.radarr.missing_movie_search.enabled = True
        cfg.radarr.missing_movie_search.cron = "0 4 * * *"
        cfg.radarr.missing_movie_search.min_release_delay_days = 2
        cfg.radarr.missing_movie_search.max_movies_per_run = 10
        cfg.radarr.missing_movie_search.movie_cooldown_days = 14

        d = config_to_dict(cfg)
        self.assertTrue(d["radarr"]["missing_movie_search"]["enabled"])
        self.assertEqual(d["radarr"]["missing_movie_search"]["cron"], "0 4 * * *")
        self.assertEqual(d["radarr"]["missing_movie_search"]["min_release_delay_days"], 2)
        self.assertEqual(d["radarr"]["missing_movie_search"]["max_movies_per_run"], 10)
        self.assertEqual(d["radarr"]["missing_movie_search"]["movie_cooldown_days"], 14)

        # Test update_config_from_dict
        update_config_from_dict(cfg, {
            "radarr": {
                "missing_movie_search": {
                    "enabled": False,
                    "cron": "0 5 * * *",
                    "max_movies_per_run": 3,
                }
            }
        })
        self.assertFalse(cfg.radarr.missing_movie_search.enabled)
        self.assertEqual(cfg.radarr.missing_movie_search.cron, "0 5 * * *")
        self.assertEqual(cfg.radarr.missing_movie_search.max_movies_per_run, 3)

    def test_is_movie_eligible_for_search(self):
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        future = (now + timedelta(days=5)).strftime("%Y-%m-%d")

        # 1. Monitored and missing, digital release 2 days ago -> eligible
        movie_eligible = {
            "id": 101,
            "title": "Eligible Movie",
            "monitored": True,
            "hasFile": False,
            "digitalRelease": yesterday,
        }
        self.assertTrue(self.client.is_movie_eligible_for_search(movie_eligible, min_release_delay_days=1))

        # 2. Not monitored -> ineligible
        movie_unmonitored = {**movie_eligible, "monitored": False}
        self.assertFalse(self.client.is_movie_eligible_for_search(movie_unmonitored))

        # 3. Already has file -> ineligible
        movie_has_file = {**movie_eligible, "hasFile": True}
        self.assertFalse(self.client.is_movie_eligible_for_search(movie_has_file))

        # 4. Release date in the future -> ineligible
        movie_future = {
            "id": 102,
            "title": "Future Movie",
            "monitored": True,
            "hasFile": False,
            "physicalRelease": future,
        }
        self.assertFalse(self.client.is_movie_eligible_for_search(movie_future, min_release_delay_days=1))

    def test_classify_movie_release(self):
        movie = {"id": 1, "title": "Inception", "cleanTitle": "inception"}

        # Approved release (approved=True, no rejections, valid size)
        rel_approved = {
            "title": "Inception.2010.1080p.WEB-DL",
            "size": 2 * 1024 * 1024 * 1024,
            "approved": True,
            "rejections": [],
        }
        status = self.client.classify_movie_release(rel_approved, movie)
        self.assertEqual(status, "tier1_eligible")

        # Quality override allowed
        rel_quality_rej = {
            "title": "Inception.2010.720p.HDTV",
            "size": 1024 * 1024 * 1024,
            "approved": False,
            "rejections": ["Existing file on disk has equal or higher quality"],
        }
        status = self.client.classify_movie_release(rel_quality_rej, movie)
        self.assertEqual(status, "tier1_eligible")

        # Sample / tiny size rejected
        rel_tiny = {
            "title": "Inception.2010.sample",
            "size": 10 * 1024 * 1024,
            "approved": True,
        }
        status = self.client.classify_movie_release(rel_tiny, movie)
        self.assertEqual(status, "rejected")

        # Ambiguous candidate needing AI
        rel_ai = {
            "title": "Inception.Origins.2010.1080p",
            "size": 2 * 1024 * 1024 * 1024,
            "approved": False,
            "rejections": ["Unknown Movie"],
        }
        status = self.client.classify_movie_release(rel_ai, movie)
        self.assertEqual(status, "tier2_candidate")

    async def test_run_missing_movie_search_tier1_match(self):
        """Tier 1: clean title/year match automatically grabbed with 0 AI tokens."""
        now = datetime.now(timezone.utc)
        released_date = (now - timedelta(days=5)).strftime("%Y-%m-%d")

        movies_mock = [
            {
                "id": 55,
                "title": "Inception",
                "cleanTitle": "inception",
                "year": 2010,
                "monitored": True,
                "hasFile": False,
                "digitalRelease": released_date,
            }
        ]

        releases_mock = [
            {
                "guid": "guid-55-tier1",
                "title": "Inception.2010.1080p.BluRay.x264",
                "size": 4 * 1024 * 1024 * 1024,
                "approved": True,
                "rejections": [],
                "indexerId": 3,
                "protocol": "torrent",
                "seeders": 15,
            }
        ]

        self.client.get_all_movies = AsyncMock(return_value=movies_mock)
        self.client.get_movie_releases = AsyncMock(return_value=releases_mock)
        self.client.grab_release = AsyncMock(return_value={"status": "ok"})

        grabbed = await self.client.run_missing_movie_search(ai_provider=self.mock_ai)
        self.assertEqual(grabbed, 1)
        self.client.grab_release.assert_called_once()
        call_kwargs = self.client.grab_release.call_args[1]
        self.assertEqual(call_kwargs["guid"], "guid-55-tier1")
        self.assertEqual(call_kwargs["indexer_id"], 3)
        # AI provider should not be called because Tier 1 succeeded!
        self.mock_ai.evaluate_missing_movie.assert_not_called()

    async def test_run_missing_movie_search_tier2_ai_match(self):
        """Tier 2: ambiguous title evaluated and approved by AI provider."""
        now = datetime.now(timezone.utc)
        released_date = (now - timedelta(days=5)).strftime("%Y-%m-%d")

        movies_mock = [
            {
                "id": 77,
                "title": "Dune Part Two",
                "cleanTitle": "duneparttwo",
                "year": 2024,
                "monitored": True,
                "hasFile": False,
                "digitalRelease": released_date,
            }
        ]

        # Candidate release does not have exact title match (e.g. Roman numeral 'II')
        releases_mock = [
            {
                "guid": "guid-77-tier2",
                "title": "Dune.II.2024.IMAX.1080p.WEB-DL",
                "size": 3 * 1024 * 1024 * 1024,
                "approved": False,
                "rejections": ["Unknown Movie"],
                "indexerId": 4,
                "protocol": "torrent",
                "seeders": 25,
            }
        ]

        self.client.get_all_movies = AsyncMock(return_value=movies_mock)
        self.client.get_movie_releases = AsyncMock(return_value=releases_mock)
        self.client.grab_release = AsyncMock(return_value={"status": "ok"})

        self.mock_ai.evaluate_missing_movie = AsyncMock(
            return_value=BackfillPackMatchResult(
                matched_guid="guid-77-tier2",
                confidence=0.95,
                reasoning="Dune II corresponds to Dune Part Two (2024).",
                model_name="MockAI",
            )
        )

        grabbed = await self.client.run_missing_movie_search(ai_provider=self.mock_ai)
        self.assertEqual(grabbed, 1)
        self.mock_ai.evaluate_missing_movie.assert_called_once()
        self.client.grab_release.assert_called_once()
        call_kwargs = self.client.grab_release.call_args[1]
        self.assertEqual(call_kwargs["guid"], "guid-77-tier2")
        self.assertEqual(call_kwargs["indexer_id"], 4)

    async def test_automation_worker_loop_handles_movie_search_event(self):
        """Worker loop responds to movie_search_event and triggers search."""
        cfg = AppConfig()
        cfg.radarr.enabled = True
        cfg.radarr.missing_movie_search.enabled = True
        cfg.radarr.missing_movie_search.cron = "0 3 * * *"

        mock_radarr = MagicMock()
        mock_radarr.run_missing_movie_search = AsyncMock(return_value=1)

        state = {
            "radarr_client": mock_radarr,
            "ai_provider": self.mock_ai,
        }

        backfill_event = asyncio.Event()
        episode_retry_event = asyncio.Event()
        movie_search_event = asyncio.Event()
        movie_search_event.set()  # Manual trigger

        import src.main as main_mod
        main_mod.running = True

        async def stop_after_brief():
            await asyncio.sleep(0.05)
            main_mod.running = False
            movie_search_event.set()

        asyncio.create_task(stop_after_brief())

        await automation_worker_loop(
            backfill_event=backfill_event,
            episode_retry_event=episode_retry_event,
            movie_search_event=movie_search_event,
            state=state,
            config=cfg,
        )

        mock_radarr.run_missing_movie_search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
