import sys
import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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

from src.cron import (
    is_valid_cron,
    normalize_cron_expr,
    interval_hours_to_cron,
    get_next_cron_run,
)
from src.config import (
    SeasonPackBackfillConfig,
    MissingEpisodeRetryConfig,
    AppConfig,
    config_to_dict,
    update_config_from_dict,
)
from src.main import backfill_worker_loop
from src.metrics import metrics


class CronAndScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        metrics.db = None
        self.mock_ai = MagicMock()
        self.mock_sonarr = MagicMock()
        self.mock_sonarr.run_season_pack_backfill = AsyncMock(return_value=1)
        self.mock_sonarr.run_missing_episode_retry = AsyncMock(return_value=2)

    def test_cron_validation_and_normalization(self):
        self.assertTrue(is_valid_cron("0 2 * * *"))
        self.assertTrue(is_valid_cron("0 */6 * * *"))
        self.assertTrue(is_valid_cron("*/15 1,3,5 1-15 1-6 0-5"))
        self.assertTrue(is_valid_cron("@daily"))
        self.assertTrue(is_valid_cron("@hourly"))
        self.assertTrue(is_valid_cron("@weekly"))

        self.assertFalse(is_valid_cron("invalid"))
        self.assertFalse(is_valid_cron("* * * *"))  # Only 4 fields
        self.assertFalse(is_valid_cron("60 * * * *"))  # Minute out of bounds
        self.assertFalse(is_valid_cron("0 24 * * *"))  # Hour out of bounds

        self.assertEqual(normalize_cron_expr("0 2 * * *"), "0 2 * * *")
        self.assertEqual(normalize_cron_expr("@daily"), "@daily")
        self.assertEqual(normalize_cron_expr("bad_cron", fallback="0 2 * * *"), "0 2 * * *")

    def test_interval_hours_to_cron(self):
        self.assertEqual(interval_hours_to_cron(1), "0 */1 * * *")
        self.assertEqual(interval_hours_to_cron(6), "0 */6 * * *")
        self.assertEqual(interval_hours_to_cron(12), "0 */12 * * *")
        self.assertEqual(interval_hours_to_cron(24), "0 2 * * *")
        self.assertEqual(interval_hours_to_cron(48), "0 2 */2 * *")

    def test_get_next_cron_run(self):
        base = datetime(2026, 10, 24, 19, 50, tzinfo=timezone.utc)
        # Daily at 2 AM
        next_daily = get_next_cron_run("0 2 * * *", base)
        self.assertEqual(next_daily, datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc))

        # Every 6 hours (0, 6, 12, 18)
        next_6h = get_next_cron_run("0 */6 * * *", base)
        self.assertEqual(next_6h, datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc))

        # Hourly
        next_hourly = get_next_cron_run("@hourly", base)
        self.assertEqual(next_hourly, datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc))

    def test_config_cron_initialization_and_legacy_fallback(self):
        # Default config uses cron
        spb = SeasonPackBackfillConfig()
        self.assertEqual(spb.cron, "0 2 * * *")

        mer = MissingEpisodeRetryConfig()
        self.assertEqual(mer.cron, "0 */6 * * *")

        # Legacy interval_hours in kwargs automatically maps to cron
        spb_legacy = SeasonPackBackfillConfig(interval_hours=12)
        self.assertEqual(spb_legacy.cron, "0 */12 * * *")

        mer_legacy = MissingEpisodeRetryConfig(interval_hours=6)
        self.assertEqual(mer_legacy.cron, "0 */6 * * *")

        # Explicit cron takes precedence
        spb_explicit = SeasonPackBackfillConfig(cron="30 4 * * *", interval_hours=12)
        self.assertEqual(spb_explicit.cron, "30 4 * * *")

    def test_config_to_dict_and_update_from_dict(self):
        cfg = AppConfig()
        cfg.sonarr.season_pack_backfill.enabled = True
        cfg.sonarr.season_pack_backfill.cron = "0 3 * * *"
        cfg.sonarr.missing_episode_retry.enabled = True
        cfg.sonarr.missing_episode_retry.cron = "0 */4 * * *"

        d = config_to_dict(cfg)
        self.assertEqual(d["sonarr"]["season_pack_backfill"]["cron"], "0 3 * * *")
        self.assertEqual(d["sonarr"]["missing_episode_retry"]["cron"], "0 */4 * * *")

        # Update from dict with new cron
        update_config_from_dict(cfg, {
            "sonarr": {
                "season_pack_backfill": {"cron": "0 5 * * *"},
                "missing_episode_retry": {"cron": "0 */8 * * *"},
            }
        })
        self.assertEqual(cfg.sonarr.season_pack_backfill.cron, "0 5 * * *")
        self.assertEqual(cfg.sonarr.missing_episode_retry.cron, "0 */8 * * *")

    async def test_backfill_worker_does_not_run_on_startup(self):
        """Worker must NOT trigger backfill or episode retry immediately on container startup."""
        cfg = AppConfig()
        cfg.sonarr.enabled = True
        cfg.sonarr.season_pack_backfill.enabled = True
        cfg.sonarr.season_pack_backfill.cron = "0 2 * * *"
        cfg.sonarr.missing_episode_retry.enabled = True
        cfg.sonarr.missing_episode_retry.cron = "0 */6 * * *"

        state = {
            "sonarr_client": self.mock_sonarr,
            "ai_provider": self.mock_ai,
        }
        backfill_event = asyncio.Event()
        episode_retry_event = asyncio.Event()

        # Run loop for a single iteration by controlling `running`
        import src.main as main_mod
        main_mod.running = True

        async def stop_after_brief():
            await asyncio.sleep(0.05)
            main_mod.running = False
            episode_retry_event.set()
            backfill_event.set()

        asyncio.create_task(stop_after_brief())

        await backfill_worker_loop(
            backfill_event=backfill_event,
            episode_retry_event=episode_retry_event,
            state=state,
            config=cfg,
        )

        # Neither backfill nor episode retry should have been executed!
        self.mock_sonarr.run_season_pack_backfill.assert_not_called()
        self.mock_sonarr.run_missing_episode_retry.assert_not_called()

        # Both tasks should be in idle state with next_run populated
        spb_status = metrics.tasks_status.get("season_pack_backfill", {})
        self.assertEqual(spb_status.get("state"), "idle")
        self.assertIsNotNone(spb_status.get("next_run"))

        mer_status = metrics.tasks_status.get("missing_episode_retry", {})
        self.assertEqual(mer_status.get("state"), "idle")
        self.assertIsNotNone(mer_status.get("next_run"))

    async def test_backfill_worker_runs_on_manual_event(self):
        """Worker runs immediately when manual backfill_event is set."""
        cfg = AppConfig()
        cfg.sonarr.enabled = True
        cfg.sonarr.season_pack_backfill.enabled = True
        cfg.sonarr.season_pack_backfill.cron = "0 2 * * *"

        state = {
            "sonarr_client": self.mock_sonarr,
            "ai_provider": self.mock_ai,
        }
        import asyncio
        backfill_event = asyncio.Event()
        backfill_event.set()  # Manual trigger
        episode_retry_event = asyncio.Event()

        import src.main as main_mod
        main_mod.running = True

        async def stop_after_brief():
            await asyncio.sleep(0.05)
            main_mod.running = False
            episode_retry_event.set()

        asyncio.create_task(stop_after_brief())

        await backfill_worker_loop(
            backfill_event=backfill_event,
            episode_retry_event=episode_retry_event,
            state=state,
            config=cfg,
        )

        self.mock_sonarr.run_season_pack_backfill.assert_called_once()
        self.mock_sonarr.run_missing_episode_retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
