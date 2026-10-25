import asyncio
from datetime import datetime, timezone, timedelta
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import time
from typing import Any, Dict, Optional, Set, Tuple
import httpx
from .config import load_config, AppConfig
from .cron import get_next_cron_run
from .ai import get_ai_provider
from .sonarr import SonarrClient
from .radarr import RadarrClient
from .metrics import metrics
from .web import WebServerRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aimportarr")

running = True


def handle_stop_signal(sig, frame):
    global running
    logger.info("Received termination signal. Shutting down gracefully...")
    running = False


def build_runtime_clients(config: AppConfig):
    """Instantiate AI provider, SonarrClient, and RadarrClient from current AppConfig."""
    log_level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)
    http_log_level = logging.DEBUG if log_level == logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(http_log_level)
    logging.getLogger("httpcore").setLevel(http_log_level)

    ai_provider = None
    try:
        ai_provider = get_ai_provider(config.ai)
    except Exception as e:
        logger.warning(f"AI provider could not be initialized yet: {e}. Configure AI settings in the Web UI.")

    sonarr_client: Optional[SonarrClient] = None
    if config.sonarr.enabled:
        if not config.sonarr.api_key or config.sonarr.api_key.startswith("YOUR_"):
            logger.warning("Sonarr is enabled but no valid api_key is configured.")
        else:
            sonarr_client = SonarrClient(
                base_url=config.sonarr.url,
                api_key=config.sonarr.api_key,
                min_confidence=config.general.min_confidence,
                dry_run=config.general.dry_run,
                reject_on_low_confidence=config.general.reject_on_low_confidence,
                extraction_config=config.extraction,
                backfill_config=config.sonarr.season_pack_backfill,
                episode_retry_config=config.sonarr.missing_episode_retry,
                path_mappings=config.path_mappings,
            )
            logger.info(f"Sonarr client configured for: {config.sonarr.url}")

    radarr_client: Optional[RadarrClient] = None
    if config.radarr.enabled:
        if not config.radarr.api_key or config.radarr.api_key.startswith("YOUR_"):
            logger.warning("Radarr is enabled but no valid api_key is configured.")
        else:
            radarr_client = RadarrClient(
                base_url=config.radarr.url,
                api_key=config.radarr.api_key,
                min_confidence=config.general.min_confidence,
                dry_run=config.general.dry_run,
                reject_on_low_confidence=config.general.reject_on_low_confidence,
                extraction_config=config.extraction,
                missing_movie_config=config.radarr.missing_movie_search,
                path_mappings=config.path_mappings,
            )
            logger.info(f"Radarr client configured for: {config.radarr.url}")

    return ai_provider, sonarr_client, radarr_client


async def fifo_worker_loop(
    fifo_queue: "asyncio.Queue[Tuple[str, Dict[str, Any], str]]",
    in_flight_keys: Set[str],
    state: Dict[str, Any],
    config: AppConfig,
):
    """
    Single-worker FIFO consumer that processes stuck queue items one at a time.
    - Prevents concurrent archive extractions from competing for disk I/O.
    - Enforces `ai_cooldown_seconds` spacing only when an item actually invokes the AI model,
      allowing 0-token fast-path items to complete at full speed without artificial delay.
    """
    last_ai_call_finished_at = 0.0

    while running:
        try:
            app_name, item, dedup_key = await asyncio.wait_for(fifo_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break

        try:
            curr_ai = state.get("ai_provider")
            client = state.get("sonarr_client") if app_name == "sonarr" else state.get("radarr_client")
            if not curr_ai or not client or not running:
                continue

            # If the previous item invoked AI, ensure cooldown has elapsed before starting the next item
            cooldown = max(0.0, float(config.general.ai_cooldown_seconds))
            if last_ai_call_finished_at > 0 and cooldown > 0:
                elapsed = time.monotonic() - last_ai_call_finished_at
                if elapsed < cooldown:
                    wait_rem = cooldown - elapsed
                    logger.debug(f"FIFO Throttler: Waiting {wait_rem:.1f}s before processing '{item.get('title', '')}'...")
                    await asyncio.sleep(wait_rem)

            ai_calls_before = metrics.total_ai_calls
            await client.process_queue_item(item, curr_ai)
            if metrics.total_ai_calls > ai_calls_before:
                last_ai_call_finished_at = time.monotonic()

        except Exception as e:
            logger.error(f"Error in FIFO worker processing {app_name} item '{item.get('title', '')}': {e}")
        finally:
            in_flight_keys.discard(dedup_key)
            fifo_queue.task_done()
            metrics.set_fifo_queue_size(len(in_flight_keys))


async def discover_and_enqueue(
    fifo_queue: "asyncio.Queue[Tuple[str, Dict[str, Any], str]]",
    in_flight_keys: Set[str],
    state: Dict[str, Any],
    config: AppConfig,
) -> int:
    """
    Poll local Sonarr/Radarr queues and push any newly stuck/eligible items into the FIFO work queue.
    Costs 0 AI tokens and skips items already in-flight or previously resolved in SQLite.
    """
    metrics.record_scan()
    curr_ai = state.get("ai_provider")
    curr_sonarr: Optional[SonarrClient] = state.get("sonarr_client")
    curr_radarr: Optional[RadarrClient] = state.get("radarr_client")
    enqueued_count = 0

    if not curr_ai:
        return 0

    # 1. Discover stuck Sonarr items
    if curr_sonarr and running:
        try:
            queue_items = await curr_sonarr.get_queue()
            seen_downloads = set()
            for item in queue_items or []:
                dl_id = item.get("downloadId")
                if dl_id:
                    if dl_id in seen_downloads:
                        continue
                    seen_downloads.add(dl_id)
                eligible, dedup_key = curr_sonarr.is_item_eligible(item)
                if eligible and dedup_key not in in_flight_keys:
                    in_flight_keys.add(dedup_key)
                    await fifo_queue.put(("sonarr", item, dedup_key))
                    enqueued_count += 1
        except (httpx.ConnectError, httpx.ConnectTimeout):
            logger.warning(
                f"Could not connect to Sonarr at '{config.sonarr.url}': connection refused or timed out."
            )
        except Exception as e:
            logger.error(f"Error polling Sonarr queue: {e}")

    # 2. Discover stuck Radarr items
    if curr_radarr and running:
        try:
            queue_items = await curr_radarr.get_queue()
            seen_downloads = set()
            for item in queue_items or []:
                dl_id = item.get("downloadId")
                if dl_id:
                    if dl_id in seen_downloads:
                        continue
                    seen_downloads.add(dl_id)
                eligible, dedup_key = curr_radarr.is_item_eligible(item)
                if eligible and dedup_key not in in_flight_keys:
                    in_flight_keys.add(dedup_key)
                    await fifo_queue.put(("radarr", item, dedup_key))
                    enqueued_count += 1
        except (httpx.ConnectError, httpx.ConnectTimeout):
            logger.warning(
                f"Could not connect to Radarr at '{config.radarr.url}': connection refused or timed out."
            )
        except Exception as e:
            logger.error(f"Error polling Radarr queue: {e}")

    metrics.set_fifo_queue_size(len(in_flight_keys))
    if enqueued_count > 0:
        logger.info(f"Enqueued {enqueued_count} item(s) into FIFO worker queue (Active queue depth: {len(in_flight_keys)})")
    return enqueued_count


async def automation_worker_loop(
    backfill_event: asyncio.Event,
    episode_retry_event: asyncio.Event,
    movie_search_event: Optional[asyncio.Event] = None,
    state: Optional[Dict[str, Any]] = None,
    config: Optional[AppConfig] = None,
    **kwargs,
):
    """
    Background worker for automated search and backfill routines:
    1. Sonarr Season Pack Backfill
    2. Sonarr Ongoing Season Missing Episode Retry
    3. Radarr Missing Movie Search
    - Runs on-demand when an event is set or automatically on each feature's configured cron schedule.
    """
    if movie_search_event is None:
        movie_search_event = asyncio.Event()
    if state is None:
        state = {}
    if config is None:
        config = AppConfig()

    next_bf_run: Optional[datetime] = None
    next_mer_run: Optional[datetime] = None
    next_mms_run: Optional[datetime] = None
    last_bf_cron: Optional[str] = None
    last_mer_cron: Optional[str] = None
    last_mms_cron: Optional[str] = None

    while running:
        try:
            manual_backfill = backfill_event.is_set()
            backfill_event.clear()
            manual_ep_retry = episode_retry_event.is_set()
            episode_retry_event.clear()
            manual_movie_search = movie_search_event.is_set()
            movie_search_event.clear()

            now_utc = datetime.now(timezone.utc)
            curr_sonarr: Optional[SonarrClient] = state.get("sonarr_client")
            curr_radarr: Optional[RadarrClient] = state.get("radarr_client")
            curr_ai = state.get("ai_provider")

            # 1. Completed Seasons: Season Pack Backfill
            bf_cfg = config.sonarr.season_pack_backfill
            bf_enabled = config.sonarr.enabled and bf_cfg.enabled
            if not bf_enabled:
                metrics.update_task_status("season_pack_backfill", state="disabled")
                next_bf_run = None
            else:
                if bf_cfg.cron != last_bf_cron:
                    last_bf_cron = bf_cfg.cron
                    next_bf_run = None

                if next_bf_run is None:
                    try:
                        next_bf_run = get_next_cron_run(bf_cfg.cron, now_utc)
                        metrics.update_task_status(
                            "season_pack_backfill",
                            state="idle",
                            next_run=next_bf_run.isoformat(),
                        )
                    except Exception as e:
                        logger.error(f"Invalid cron expression for season_pack_backfill '{bf_cfg.cron}': {e}")
                        metrics.update_task_status(
                            "season_pack_backfill",
                            state="idle",
                            progress=f"Invalid cron: {bf_cfg.cron}",
                        )

            bf_due = bf_enabled and next_bf_run is not None and now_utc >= next_bf_run

            if (manual_backfill or bf_due) and curr_sonarr and running and bf_enabled:
                t0 = time.monotonic()
                metrics.update_task_status(
                    "season_pack_backfill",
                    state="running",
                    last_run=now_utc.isoformat(),
                )
                try:
                    grabbed = await curr_sonarr.run_season_pack_backfill(ai_provider=curr_ai)
                    dur = time.monotonic() - t0
                    next_bf_run = get_next_cron_run(bf_cfg.cron, datetime.now(timezone.utc))
                    metrics.update_task_status(
                        "season_pack_backfill",
                        state="idle",
                        duration_sec=dur,
                        items_grabbed=grabbed,
                        next_run=next_bf_run.isoformat(),
                    )
                except Exception as e:
                    logger.error(f"Error during Sonarr Season Pack Backfill: {e}")
                    next_bf_run = get_next_cron_run(bf_cfg.cron, datetime.now(timezone.utc))
                    metrics.update_task_status(
                        "season_pack_backfill",
                        state="idle",
                        progress=f"Error: {e}",
                        next_run=next_bf_run.isoformat(),
                    )

            # 2. Ongoing Seasons: Missing Episode Retry
            mer_cfg = config.sonarr.missing_episode_retry
            mer_enabled = config.sonarr.enabled and mer_cfg.enabled
            now_utc = datetime.now(timezone.utc)
            if not mer_enabled:
                metrics.update_task_status("missing_episode_retry", state="disabled")
                next_mer_run = None
            else:
                if mer_cfg.cron != last_mer_cron:
                    last_mer_cron = mer_cfg.cron
                    next_mer_run = None

                if next_mer_run is None:
                    try:
                        next_mer_run = get_next_cron_run(mer_cfg.cron, now_utc)
                        metrics.update_task_status(
                            "missing_episode_retry",
                            state="idle",
                            next_run=next_mer_run.isoformat(),
                        )
                    except Exception as e:
                        logger.error(f"Invalid cron expression for missing_episode_retry '{mer_cfg.cron}': {e}")
                        metrics.update_task_status(
                            "missing_episode_retry",
                            state="idle",
                            progress=f"Invalid cron: {mer_cfg.cron}",
                        )

            mer_due = mer_enabled and next_mer_run is not None and now_utc >= next_mer_run

            if (manual_ep_retry or mer_due) and curr_sonarr and running and mer_enabled:
                t0 = time.monotonic()
                metrics.update_task_status(
                    "missing_episode_retry",
                    state="running",
                    last_run=now_utc.isoformat(),
                )
                try:
                    grabbed = await curr_sonarr.run_missing_episode_retry(ai_provider=curr_ai)
                    dur = time.monotonic() - t0
                    next_mer_run = get_next_cron_run(mer_cfg.cron, datetime.now(timezone.utc))
                    metrics.update_task_status(
                        "missing_episode_retry",
                        state="idle",
                        duration_sec=dur,
                        items_grabbed=grabbed,
                        next_run=next_mer_run.isoformat(),
                    )
                except Exception as e:
                    logger.error(f"Error during Sonarr Missing Episode Retry: {e}")
                    next_mer_run = get_next_cron_run(mer_cfg.cron, datetime.now(timezone.utc))
                    metrics.update_task_status(
                        "missing_episode_retry",
                        state="idle",
                        progress=f"Error: {e}",
                        next_run=next_mer_run.isoformat(),
                    )

            # 3. Missing Movies: Missing Movie Search (Radarr)
            mms_cfg = config.radarr.missing_movie_search
            mms_enabled = config.radarr.enabled and mms_cfg.enabled
            now_utc = datetime.now(timezone.utc)
            if not mms_enabled:
                metrics.update_task_status("missing_movie_search", state="disabled")
                next_mms_run = None
            else:
                if mms_cfg.cron != last_mms_cron:
                    last_mms_cron = mms_cfg.cron
                    next_mms_run = None

                if next_mms_run is None:
                    try:
                        next_mms_run = get_next_cron_run(mms_cfg.cron, now_utc)
                        metrics.update_task_status(
                            "missing_movie_search",
                            state="idle",
                            next_run=next_mms_run.isoformat(),
                        )
                    except Exception as e:
                        logger.error(f"Invalid cron expression for missing_movie_search '{mms_cfg.cron}': {e}")
                        metrics.update_task_status(
                            "missing_movie_search",
                            state="idle",
                            progress=f"Invalid cron: {mms_cfg.cron}",
                        )

            mms_due = mms_enabled and next_mms_run is not None and now_utc >= next_mms_run

            if (manual_movie_search or mms_due) and curr_radarr and running and mms_enabled:
                t0 = time.monotonic()
                metrics.update_task_status(
                    "missing_movie_search",
                    state="running",
                    last_run=now_utc.isoformat(),
                )
                try:
                    grabbed = await curr_radarr.run_missing_movie_search(ai_provider=curr_ai)
                    dur = time.monotonic() - t0
                    next_mms_run = get_next_cron_run(mms_cfg.cron, datetime.now(timezone.utc))
                    metrics.update_task_status(
                        "missing_movie_search",
                        state="idle",
                        duration_sec=dur,
                        items_grabbed=grabbed,
                        next_run=next_mms_run.isoformat(),
                    )
                except Exception as e:
                    logger.error(f"Error during Radarr Missing Movie Search: {e}")
                    next_mms_run = get_next_cron_run(mms_cfg.cron, datetime.now(timezone.utc))
                    metrics.update_task_status(
                        "missing_movie_search",
                        state="idle",
                        progress=f"Error: {e}",
                        next_run=next_mms_run.isoformat(),
                    )

            for _ in range(5):
                if not running or backfill_event.is_set() or episode_retry_event.is_set() or movie_search_event.is_set():
                    break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Unexpected error in automation_worker_loop: {e}")
            await asyncio.sleep(5.0)


# Backward compatibility alias
backfill_worker_loop = automation_worker_loop


async def main():
    global running
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    logger.info("Starting AImportarr - AI-Powered *arr Media Importer (Sonarr & Radarr)")
    config = load_config()

    log_level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    log_dir = Path("/config") if Path("/config").is_dir() else Path(config.database.path).parent
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "aimportarr.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(file_handler)
        logger.info(f"File logging enabled: {log_file}")
    except Exception as e:
        logger.warning(f"Could not initialize file logger in '{log_dir}': {e}")

    mode_str = "DRY RUN (Simulation)" if config.general.dry_run else "LIVE (Auto-Import & Blocklist)"
    low_conf_str = (
        "Hands-Off (Blocklist & Re-Search, Keep in Client)"
        if config.general.reject_on_low_confidence
        else "Conservative (Leave in Queue)"
    )
    logger.info(
        f"Configuration loaded | Mode: {mode_str} | Low-Confidence Policy: {low_conf_str} | Sync Interval: {config.general.check_interval_seconds}s | AI Cooldown: {config.general.ai_cooldown_seconds}s"
    )
    logger.info(
        f"Archive Extractor: {'Enabled' if config.extraction.enabled else 'Disabled'} (Delete after import: {config.extraction.delete_after_import})"
    )

    metrics.init_database(
        db_path=config.database.path,
        retention_days=config.database.retention_days,
    )

    loop = asyncio.get_running_loop()
    scan_event = asyncio.Event()
    backfill_event = asyncio.Event()
    episode_retry_event = asyncio.Event()
    movie_search_event = asyncio.Event()
    fifo_queue: asyncio.Queue[Tuple[str, Dict[str, Any], str]] = asyncio.Queue()
    in_flight_keys: Set[str] = set()

    state: Dict[str, Any] = {}
    ai_provider, sonarr_client, radarr_client = build_runtime_clients(config)
    state["ai_provider"] = ai_provider
    state["sonarr_client"] = sonarr_client
    state["radarr_client"] = radarr_client

    def trigger_scan_threadsafe(source: str = "manual"):
        if source == "backfill":
            logger.info("Immediate Season Pack Backfill check requested from Web UI.")
            loop.call_soon_threadsafe(backfill_event.set)
            return
        if source == "episode_retry":
            logger.info("Immediate Missing Episode Retry check requested from Web UI.")
            loop.call_soon_threadsafe(episode_retry_event.set)
            return
        if source == "movie_search":
            logger.info("Immediate Missing Movie Search check requested from Web UI.")
            loop.call_soon_threadsafe(movie_search_event.set)
            return
        logger.debug(f"Immediate queue scan requested (source: {source})")
        loop.call_soon_threadsafe(scan_event.set)
        if source.startswith("retry"):
            if config.sonarr.season_pack_backfill.enabled:
                loop.call_soon_threadsafe(backfill_event.set)
            if config.sonarr.missing_episode_retry.enabled:
                loop.call_soon_threadsafe(episode_retry_event.set)
            if config.radarr.missing_movie_search.enabled:
                loop.call_soon_threadsafe(movie_search_event.set)

    def on_config_updated(updated_config: AppConfig):
        logger.info("Hot-reloading runtime clients from updated Web UI configuration...")
        new_ai, new_sonarr, new_radarr = build_runtime_clients(updated_config)
        state["ai_provider"] = new_ai
        state["sonarr_client"] = new_sonarr
        state["radarr_client"] = new_radarr
        trigger_scan_threadsafe("config_updated")

    web_server = None
    if config.web.enabled:
        try:
            web_server = WebServerRunner(
                host=config.web.host,
                port=config.web.port,
                metrics_instance=metrics,
                config_instance=config,
                on_config_updated=on_config_updated,
                on_trigger_scan=trigger_scan_threadsafe,
            )
            web_server.start()
        except Exception as we:
            logger.error(f"Failed to start Web UI server on {config.web.host}:{config.web.port}: {we}")

    # Start the single-worker FIFO consumer task and automated search/backfill worker task
    worker_task = asyncio.create_task(
        fifo_worker_loop(fifo_queue, in_flight_keys, state, config)
    )
    automation_task = asyncio.create_task(
        automation_worker_loop(backfill_event, episode_retry_event, movie_search_event, state, config)
    )

    try:
        while running:
            scan_event.clear()
            await discover_and_enqueue(fifo_queue, in_flight_keys, state, config)

            # Wait until either check_interval_seconds elapses OR an instant trigger (Webhook / Retry / Scan Now) fires
            interval = max(5, int(config.general.check_interval_seconds))
            for _ in range(interval):
                if not running or scan_event.is_set():
                    break
                try:
                    await asyncio.wait_for(scan_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    pass
    finally:
        worker_task.cancel()
        automation_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        try:
            await automation_task
        except asyncio.CancelledError:
            pass
        if web_server:
            web_server.stop()

    logger.info("AImportarr has stopped.")


if __name__ == "__main__":
    asyncio.run(main())
